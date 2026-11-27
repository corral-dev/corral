"""Cloud-maintained capability registry with ETag caching and bundled offline fallback.

Strictly read-only static HTTP GET (zero telemetry, never uploads local
inventory or session data). Ships a vetted bundled snapshot inside the package
so air-gapped or offline installations work immediately on day 1.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from corral_workflow.errors import EXIT_CONFLICT, EXIT_USAGE, WorkflowError
from corral_workflow.store import WorkflowStore, compute_version, utc_iso

ENV_REGISTRY_URL = "CORRAL_WORKFLOW_REGISTRY_URL"
ENV_REGISTRY_OFFLINE = "CORRAL_WORKFLOW_REGISTRY_OFFLINE"
DEFAULT_REGISTRY_URL = (
    "https://raw.githubusercontent.com/corral-dev/corral/main/cli/src/corral/workflow/bundled_registry.json"
)
DEFAULT_SYNC_TTL_SECONDS = 24 * 3600
_FETCH_TIMEOUT_SECONDS = 3.0
_MAX_REGISTRY_BYTES = 1_000_000

BUNDLED_REGISTRY_PATH = Path(__file__).with_name("bundled_registry.json")


def registry_cache_path() -> Path:
    cache_env = os.environ.get("CORRAL_CACHE_DIR", "").strip()
    if cache_env:
        base = Path(cache_env).expanduser()
    else:
        xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
        base = (Path(xdg).expanduser() if xdg else Path.home() / ".cache") / "corral"
    return base / "workflow-registry.json"


def is_offline_mode() -> bool:
    return os.environ.get(ENV_REGISTRY_OFFLINE, "").strip().lower() in {"1", "true", "yes", "on"}


def configured_registry_url(store: WorkflowStore | None = None) -> tuple[str, str]:
    """Return ``(url, url_source)`` where source is ``env | user | default``."""
    env_url = os.environ.get(ENV_REGISTRY_URL, "").strip()
    if env_url:
        return env_url, "env"
    if store is not None:
        saved = store.get_registry_state("custom_url")
        if isinstance(saved, dict) and isinstance(saved.get("url"), str) and saved["url"].strip():
            return saved["url"].strip(), "user"
    return DEFAULT_REGISTRY_URL, "default"


def _validate_manifest(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("registry manifest must be a JSON object")
    if data.get("schema_version") != 1:
        raise ValueError(f"unsupported registry schema_version: {data.get('schema_version')!r}")
    if not isinstance(data.get("capabilities"), dict) or not isinstance(data.get("families"), dict):
        raise ValueError("registry manifest must define 'capabilities' and 'families' objects")
    return data


def load_bundled_registry() -> dict[str, Any]:
    raw = BUNDLED_REGISTRY_PATH.read_text(encoding="utf-8")
    data = _validate_manifest(json.loads(raw))
    return {
        "manifest": data,
        "source": "bundled",
        "registry_version": str(data.get("registry_version") or "bundled"),
        "published_at": data.get("published_at"),
        "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
        "fetched_at": None,
        "etag": None,
    }


def load_active_registry(store: WorkflowStore | None = None) -> dict[str, Any]:
    """Offline-first registry load: cached cloud snapshot if valid, else bundled snapshot."""
    cache_file = registry_cache_path()
    if cache_file.is_file():
        try:
            envelope = json.loads(cache_file.read_text(encoding="utf-8"))
            manifest = _validate_manifest(envelope.get("manifest"))
            return {
                "manifest": manifest,
                "source": "cloud_cached",
                "registry_version": str(manifest.get("registry_version") or "unknown"),
                "published_at": manifest.get("published_at"),
                "sha256": envelope.get("sha256"),
                "fetched_at": envelope.get("fetched_at"),
                "fetched_at_utc": utc_iso(envelope["fetched_at"]) if envelope.get("fetched_at") else None,
                "etag": envelope.get("etag"),
                "url": envelope.get("url"),
            }
        except (OSError, ValueError, TypeError):
            pass
    bundled = load_bundled_registry()
    url, url_source = configured_registry_url(store)
    bundled["url"] = url
    bundled["url_source"] = url_source
    return bundled


def sync_registry(
    store: WorkflowStore | None = None,
    *,
    force: bool = False,
    timeout: float = _FETCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Conditionally fetch the static cloud capability manifest via ETag."""
    url, url_source = configured_registry_url(store)
    current = load_active_registry(store)
    if is_offline_mode():
        return {
            **current,
            "synced": False,
            "status": "offline_disabled",
            "url": url,
            "url_source": url_source,
        }
    fetched_at = current.get("fetched_at")
    if not force and isinstance(fetched_at, (int, float)) and (time.time() - fetched_at) < DEFAULT_SYNC_TTL_SECONDS:
        return {
            **current,
            "synced": False,
            "status": "fresh_cache",
            "url": url,
            "url_source": url_source,
        }

    headers = {"Accept": "application/json", "User-Agent": "corral-workflow/1"}
    if not force and current.get("etag"):
        headers["If-None-Match"] = str(current["etag"])

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(_MAX_REGISTRY_BYTES + 1)
            if len(raw) > _MAX_REGISTRY_BYTES:
                raise ValueError("registry manifest exceeds 1 MiB safety limit")
            text = raw.decode("utf-8")
            manifest = _validate_manifest(json.loads(text))
            etag = resp.headers.get("ETag")
            now = time.time()
            sha = hashlib.sha256(raw).hexdigest()[:16]
            cache_file = registry_cache_path()
            cache_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            payload = {
                "manifest": manifest,
                "etag": etag,
                "sha256": sha,
                "fetched_at": now,
                "url": url,
            }
            tmp = cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(cache_file)
            return {
                "manifest": manifest,
                "source": "cloud_cached",
                "registry_version": str(manifest.get("registry_version") or "unknown"),
                "published_at": manifest.get("published_at"),
                "sha256": sha,
                "fetched_at": now,
                "fetched_at_utc": utc_iso(now),
                "etag": etag,
                "url": url,
                "url_source": url_source,
                "synced": True,
                "status": "updated",
            }
    except urllib.error.HTTPError as exc:
        try:
            code = exc.code
            reason = exc.reason
        finally:
            exc.close()
        if code == 304 and current.get("source") == "cloud_cached":
            now = time.time()
            cache_file = registry_cache_path()
            try:
                existing = json.loads(cache_file.read_text(encoding="utf-8"))
                existing["fetched_at"] = now
                cache_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except (OSError, ValueError):
                pass
            return {
                **current,
                "fetched_at": now,
                "fetched_at_utc": utc_iso(now),
                "url": url,
                "url_source": url_source,
                "synced": True,
                "status": "not_modified",
            }
        return {
            **current,
            "url": url,
            "url_source": url_source,
            "synced": False,
            "status": "fetch_failed",
            "fetch_error": f"HTTP {code}: {reason}",
        }
    except (OSError, TimeoutError, ValueError, UnicodeError, urllib.error.URLError) as exc:
        return {
            **current,
            "url": url,
            "url_source": url_source,
            "synced": False,
            "status": "fetch_failed",
            "fetch_error": str(exc),
        }


def registry_url_version(store: WorkflowStore) -> str:
    url, _ = configured_registry_url(store)
    return compute_version({"url": url})


def set_registry_url(
    store: WorkflowStore,
    url: str,
    *,
    expected_version: str | None,
    dry_run: bool = False,
    actor: str = "local-user",
) -> dict[str, Any]:
    cleaned = url.strip()
    if not (
        cleaned.startswith("https://")
        or cleaned.startswith("http://127.0.0.1")
        or cleaned.startswith("http://localhost")
    ):
        raise WorkflowError(
            "usage_error",
            "Registry URL must use https:// (or loopback http://127.0.0.1 / http://localhost for local testing).",
            EXIT_USAGE,
        )
    current_url, current_source = configured_registry_url(store)
    current_version = compute_version({"url": current_url})
    if not expected_version:
        raise WorkflowError(
            "confirmation_required",
            f"Updating registry URL requires --version {current_version}.",
            EXIT_USAGE,
            context={"current_url": current_url, "current_version": current_version},
        )
    if expected_version != current_version:
        raise WorkflowError(
            "conflict",
            f"Registry URL version mismatch: expected {expected_version}, current is {current_version}.",
            EXIT_CONFLICT,
            context={"current_url": current_url, "current_version": current_version},
        )
    new_version = compute_version({"url": cleaned})
    changed = cleaned != current_url
    if not dry_run and changed:
        store.set_registry_state("custom_url", {"url": cleaned}, actor=actor)
    return {
        "url": cleaned,
        "previous_url": current_url,
        "previous_source": current_source,
        "changed": changed if not dry_run else False,
        "would_change": changed,
        "version": new_version if not dry_run else current_version,
        "dry_run": dry_run,
    }


def classify_model_family(
    manifest: dict[str, Any],
    *,
    runtime: str | None,
    model: str | None,
    kind: str = "runtime",
) -> str:
    """Map a concrete ``(runtime, model)`` pair to its canonical cloud family ID."""
    families = manifest.get("families") or {}
    if isinstance(model, str) and model.strip():
        needle = model.strip().lower()
        # Pick the longest matching pattern so e.g. 'gpt-5-mini' beats 'gpt-5'.
        best_family: str | None = None
        best_len = -1
        for family_id, info in families.items():
            patterns = info.get("patterns") if isinstance(info, dict) else None
            if not isinstance(patterns, list):
                continue
            for pat in patterns:
                if not isinstance(pat, str) or not pat:
                    continue
                low = pat.lower()
                if low in needle and len(low) > best_len:
                    best_len = len(low)
                    best_family = family_id
        if best_family is not None:
            return best_family
    if kind == "gateway":
        return "gateway-general"
    if runtime:
        default_family = f"runtime-default-{runtime}"
        if default_family in families:
            return default_family
    return "runtime-default-unknown"
