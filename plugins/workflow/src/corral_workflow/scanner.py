"""Zero-quota local runtime and model inventory scanner.

Discovers installed Corral-supported coding runtimes (6 active runtimes) plus
configured LLM Gateway aliases by inspecting local executables, version flags
and local configuration files. Never issues paid model requests during scanning.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from corral_workflow.registry import classify_model_family, load_active_registry
from corral_workflow.state_bridge import (
    ACTIVE_RUNTIME_IDS,
    fetch_model_aliases,
    gateway_key,
    gateway_url,
)
from corral_workflow.store import WorkflowStore, utc_iso

DEFAULT_SCAN_TTL_SECONDS = 6 * 3600

_RUNTIME_BINARIES: dict[str, tuple[str, ...]] = {
    "claude": ("claude",),
    "codex": ("codex",),
    "opencode": ("opencode",),
    "cursor": ("cursor-agent",),
    "pi": ("pi",),
    "antigravity": ("agy",),
}

# Known local config/auth paths per runtime (relative to home directory).
_RUNTIME_WATCH_FILES: dict[str, tuple[str, ...]] = {
    "claude": (".claude/settings.json", ".claude/.credentials.json", ".claude.json"),
    "codex": (".codex/config.toml", ".codex/auth.json"),
    "opencode": (".config/opencode/opencode.json", ".config/opencode/config.json"),
    "cursor": (".cursor/cli-config.json", ".cursor/config.json"),
    "pi": (".pi/agent/models.json", ".pi/agent/settings.json"),
    "antigravity": (".gemini/antigravity-cli/settings.json",),
}

_RUNTIME_MODEL_OVERRIDE_DEFAULT: dict[str, bool] = {
    "claude": True,
    "codex": True,
    "opencode": False,  # OpenCode root interactive CLI rejects --model on verified releases
    "cursor": True,
    "pi": True,
    "antigravity": False,
}


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", text.strip().lower()).strip("-._")
    return cleaned[:64] or "default"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_toml_model(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        import tomllib  # Python 3.11+

        parsed = tomllib.loads(raw)
        model = parsed.get("model")
        return str(model).strip() if isinstance(model, str) and model.strip() else None
    except Exception:
        match = re.search(r'^\s*model\s*=\s*"([^"]+)"', raw, re.MULTILINE)
        return match.group(1).strip() if match else None


def _probe_cli_version(executable: str, timeout: float = 2.0) -> str | None:
    try:
        proc = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        out = (proc.stdout or proc.stderr or "").strip()
        if not out:
            return None
        first_line = out.splitlines()[0].strip()
        return first_line[:120] if first_line else None
    except (OSError, subprocess.SubprocessError):
        return None


def _detect_runtime_models(
    runtime: str,
    home: Path,
    env: dict[str, str],
) -> tuple[list[tuple[str | None, str]], str, str | None]:
    """Return ``([(model_or_none, source)], auth_hint, primary_source_path)``."""
    models: list[tuple[str | None, str]] = []
    auth_hint = "unknown"
    source_path: str | None = None

    if runtime == "claude":
        settings = home / ".claude" / "settings.json"
        data = _read_json(settings)
        if isinstance(data, dict):
            source_path = str(settings)
            m = data.get("model")
            if isinstance(m, str) and m.strip():
                models.append((m.strip(), "settings.json"))
        if (
            (home / ".claude" / ".credentials.json").is_file()
            or (home / ".claude.json").is_file()
            or env.get("ANTHROPIC_API_KEY")
        ):
            auth_hint = "present"
        if not models:
            models.append((None, "runtime_default"))

    elif runtime == "codex":
        cfg = home / ".codex" / "config.toml"
        model = _read_toml_model(cfg)
        if model:
            source_path = str(cfg)
            models.append((model, "config.toml"))
        if (home / ".codex" / "auth.json").is_file() or env.get("OPENAI_API_KEY"):
            auth_hint = "present"
        if not models:
            models.append((None, "runtime_default"))

    elif runtime == "opencode":
        for rel in (".config/opencode/opencode.json", ".config/opencode/config.json"):
            cfg = home / rel
            data = _read_json(cfg)
            if isinstance(data, dict):
                source_path = str(cfg)
                m = data.get("model")
                if isinstance(m, str) and m.strip():
                    models.append((m.strip(), cfg.name))
                providers = data.get("provider") or data.get("providers")
                if isinstance(providers, dict) and providers:
                    auth_hint = "present"
                break
        if not models:
            models.append((None, "runtime_default"))

    elif runtime == "cursor":
        for rel in (".cursor/cli-config.json", ".cursor/config.json"):
            cfg = home / rel
            data = _read_json(cfg)
            if isinstance(data, dict):
                source_path = str(cfg)
                m = data.get("model") or data.get("defaultModel")
                if isinstance(m, str) and m.strip():
                    models.append((m.strip(), cfg.name))
                break
        if env.get("CURSOR_API_KEY") or (home / ".cursor").is_dir():
            auth_hint = "present"
        if not models:
            models.append((None, "runtime_default"))

    elif runtime == "pi":
        settings_path = home / ".pi" / "agent" / "settings.json"
        models_path = home / ".pi" / "agent" / "models.json"
        settings = _read_json(settings_path)
        if isinstance(settings, dict):
            source_path = str(settings_path)
            provider = settings.get("defaultProvider")
            model_id = settings.get("defaultModel")
            if isinstance(model_id, str) and model_id.strip():
                full = f"{provider}/{model_id}" if isinstance(provider, str) and provider.strip() else model_id.strip()
                models.append((full, "settings.json"))
                auth_hint = "present"
        catalog = _read_json(models_path)
        if isinstance(catalog, dict):
            source_path = source_path or str(models_path)
            providers = catalog.get("providers")
            if isinstance(providers, dict):
                if providers:
                    auth_hint = "present"
                seen = {m for m, _ in models}
                for prov_name, prov_cfg in providers.items():
                    if not isinstance(prov_cfg, dict):
                        continue
                    plist = prov_cfg.get("models")
                    if not isinstance(plist, list):
                        continue
                    for entry in plist[:12]:
                        mid = (
                            entry.get("id") if isinstance(entry, dict) else (entry if isinstance(entry, str) else None)
                        )
                        if not isinstance(mid, str) or not mid.strip():
                            continue
                        full = f"{prov_name}/{mid.strip()}"
                        if full not in seen:
                            seen.add(full)
                            models.append((full, "models.json"))
        if not models:
            models.append((None, "runtime_default"))

    elif runtime == "antigravity":
        cfg = home / ".gemini" / "antigravity-cli" / "settings.json"
        data = _read_json(cfg)
        if isinstance(data, dict):
            source_path = str(cfg)
            m = data.get("model") or data.get("defaultModel")
            if isinstance(m, str) and m.strip():
                models.append((m.strip(), "settings.json"))
            auth_hint = "present"
        if not models:
            models.append((None, "runtime_default"))

    return models, auth_hint, source_path


def _supports_model_override(runtime: str) -> bool:
    try:
        from agent_session_control.runtime import runtime_profile

        prof = runtime_profile(runtime)
        return bool(prof.get("model_selection", prof.get("model_supported", False)))
    except Exception:
        pass
    return _RUNTIME_MODEL_OVERRIDE_DEFAULT.get(runtime, False)


def _discover_gateway_aliases() -> tuple[list[str], str | None]:
    try:
        url = gateway_url()
        key = gateway_key()
        if not url or not key:
            return [], None
        aliases = fetch_model_aliases()
        return aliases, url
    except Exception:
        return [], None


def scan_local_inventory(
    store: WorkflowStore,
    *,
    runtimes: list[str] | None = None,
    dry_run: bool = False,
    probe_versions: bool = True,
    probe_gateway: bool = True,
    home_dir: Path | None = None,
    env: dict[str, str] | None = None,
    which_fn: Callable[[str], str | None] | None = None,
    version_fn: Callable[[str], str | None] | None = None,
    gateway_models_fn: Callable[[], tuple[list[str], str | None]] | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    """Scan installed runtimes and locally available models (zero paid quota)."""
    home = (home_dir or Path.home()).expanduser()
    proc_env = env if env is not None else dict(os.environ)
    locate = which_fn or shutil.which
    get_version = version_fn or _probe_cli_version
    get_gateway = gateway_models_fn or _discover_gateway_aliases

    registry_bundle = load_active_registry(store)
    manifest = registry_bundle["manifest"]

    selected = [r for r in (runtimes or sorted(ACTIVE_RUNTIME_IDS)) if r in ACTIVE_RUNTIME_IDS]
    now = time.time()
    discovered: list[dict[str, Any]] = []
    runtime_summary: list[dict[str, Any]] = []
    failures: dict[str, str] = {}

    for runtime in selected:
        binaries = _RUNTIME_BINARIES.get(runtime, (runtime,))
        found_path: str | None = None
        for exe in binaries:
            candidate = locate(exe)
            if candidate:
                found_path = candidate
                break

        supports_override = _supports_model_override(runtime)
        if not found_path:
            runtime_summary.append(
                {
                    "runtime": runtime,
                    "available": False,
                    "executable": None,
                    "cli_version": None,
                    "supports_model_override": supports_override,
                    "models_found": 0,
                }
            )
            continue

        cli_version = get_version(found_path) if probe_versions else None
        try:
            model_entries, auth_hint, src_path = _detect_runtime_models(runtime, home, proc_env)
        except Exception as exc:
            failures[runtime] = str(exc)
            model_entries, auth_hint, src_path = [(None, "fallback")], "unknown", None

        # Always register a canonical `<runtime>-default` target so runtime-default
        # capability routing works even when explicit `--model` is unsupported.
        primary_model = model_entries[0][0] if model_entries else None
        default_family = classify_model_family(manifest, runtime=runtime, model=primary_model, kind="runtime")
        discovered.append(
            {
                "id": f"{runtime}-default",
                "kind": "runtime",
                "runtime": runtime,
                "model": None,
                "family": default_family,
                "available": True,
                "supports_model_override": supports_override,
                "auth_hint": auth_hint,
                "cli_version": cli_version,
                "source_path": src_path or found_path,
                "evidence": {
                    "executable": found_path,
                    "detected_default_model": primary_model,
                    "model_source": model_entries[0][1] if model_entries else "runtime_default",
                },
                "scanned_at": now,
            }
        )

        # If the runtime supports explicit `--model` per launch, also register concrete model targets.
        model_count = 1 if primary_model else 0
        if supports_override:
            seen_ids = {f"{runtime}-default"}
            for model_id, msrc in model_entries:
                if not model_id:
                    continue
                target_id = f"{runtime}-{_slug(model_id)}"
                if target_id in seen_ids:
                    continue
                seen_ids.add(target_id)
                family = classify_model_family(manifest, runtime=runtime, model=model_id, kind="runtime")
                discovered.append(
                    {
                        "id": target_id,
                        "kind": "runtime",
                        "runtime": runtime,
                        "model": model_id,
                        "family": family,
                        "available": True,
                        "supports_model_override": True,
                        "auth_hint": auth_hint,
                        "cli_version": cli_version,
                        "source_path": src_path or found_path,
                        "evidence": {"executable": found_path, "model_source": msrc},
                        "scanned_at": now,
                    }
                )
                model_count += 1

        runtime_summary.append(
            {
                "runtime": runtime,
                "available": True,
                "executable": found_path,
                "cli_version": cli_version,
                "supports_model_override": supports_override,
                "auth_hint": auth_hint,
                "models_found": model_count,
            }
        )

    gateway_aliases: list[str] = []
    if probe_gateway:
        aliases, gw_url = get_gateway()
        # Prefer curated top-level gateway aliases (no upstream '/' prefix) so
        # passthrough catalogs (e.g. hundreds of openrouter/* rows) never flood the catalog.
        semantic_aliases = [a for a in aliases if "/" not in a]
        selected_aliases = (semantic_aliases or aliases)[:12]
        gateway_aliases = list(selected_aliases)
        for alias in selected_aliases:
            target_id = f"gw-{_slug(alias)}"
            family = classify_model_family(manifest, runtime=None, model=alias, kind="gateway")
            discovered.append(
                {
                    "id": target_id,
                    "kind": "gateway",
                    "runtime": None,
                    "model": alias,
                    "family": family,
                    "available": True,
                    "supports_model_override": True,
                    "auth_hint": "present",
                    "cli_version": None,
                    "source_path": gw_url,
                    "evidence": {"gateway_url": gw_url, "model_source": "/v1/models"},
                    "scanned_at": now,
                }
            )

    if not dry_run:
        store.replace_inventory_targets(discovered, actor=actor)

    return {
        "scanned_at": now,
        "scanned_at_utc": utc_iso(now),
        "dry_run": dry_run,
        "runtimes": runtime_summary,
        "gateway_models": gateway_aliases,
        "targets_discovered": len(discovered),
        "targets": discovered,
        "failures": failures,
    }


def _config_newer_than(last_scan: float, home: Path) -> bool:
    for rels in _RUNTIME_WATCH_FILES.values():
        for rel in rels:
            path = home / rel
            try:
                if path.is_file() and path.stat().st_mtime > last_scan:
                    return True
            except OSError:
                continue
    return False


def ensure_inventory_fresh(
    store: WorkflowStore,
    *,
    ttl_seconds: float = DEFAULT_SCAN_TTL_SECONDS,
    force_refresh: bool = False,
    home_dir: Path | None = None,
    probe_versions: bool = True,
    probe_gateway: bool = True,
) -> list[dict[str, Any]]:
    """Return cached local inventory, auto-scanning on cold start or stale TTL/mtime."""
    existing = store.list_inventory_targets()
    last_scan = store.latest_inventory_scan_time()
    home = (home_dir or Path.home()).expanduser()
    stale = (
        force_refresh
        or not existing
        or last_scan is None
        or (time.time() - last_scan) >= ttl_seconds
        or _config_newer_than(last_scan, home)
    )
    if stale:
        scan_local_inventory(
            store,
            probe_versions=probe_versions,
            probe_gateway=probe_gateway,
            home_dir=home,
        )
        return store.list_inventory_targets()
    return existing
