"""Bundled agent skills (`corral skills …`): install into each runtime's own skill root.

Independent of any skill manager: roots are ordinary directories, resolved and
deduplicated so one physical directory gets one copy. Corral only ever touches
its bundled names and only content it can prove it wrote (hash manifest).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

BUNDLE_NAMES = ("corral-search", "corral-delegate")
_BUNDLE_DIR = Path(__file__).parent / "skills"


def _home() -> Path:
    return Path.home()


def manifest_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(_home() / ".config")
    return Path(base) / "corral" / "skills" / "registrations.json"


def bundle_text(name: str) -> str:
    return (_BUNDLE_DIR / name / "SKILL.md").read_text(encoding="utf-8")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def runtime_roots() -> list[dict]:
    """Each active runtime's user skill root and whether that runtime is installed."""
    from corral.runtime import default_registry

    home = _home()
    shared = home / ".agents" / "skills"
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    pi_dir = Path(os.environ.get("PI_CODING_AGENT_DIR") or home / ".pi" / "agent")
    roots = {
        "claude": Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "skills",
        "codex": shared if shared.is_dir() else Path(os.environ.get("CODEX_HOME") or home / ".codex") / "skills",
        "opencode": xdg / "opencode" / "skills",
        "cursor": home / ".cursor" / "skills",
        "pi": shared if shared.is_dir() else pi_dir / "skills",
    }
    registry = default_registry()
    out = []
    for runtime_id, root in roots.items():
        try:
            available = bool(registry.get(runtime_id).is_available())
        except Exception:  # noqa: BLE001 - an unknown adapter is simply not targeted
            available = False
        resolved = Path(os.path.realpath(root))
        out.append({"runtime": runtime_id, "root": str(root), "resolved_root": str(resolved),
                    "available": available, "root_exists": resolved.is_dir()})
    return out


def _load_manifest() -> dict:
    try:
        data = json.loads(manifest_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_manifest(data: dict) -> None:
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".registrations.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _ownership(manifest: dict) -> dict:
    owned = {path: dict(record) for path, record in manifest.get("installed", {}).items()}
    for path, record in manifest.get("pending", {}).items():
        owned.setdefault(path, {}).update(pending_hash=record["hash"])
    return owned


@contextmanager
def _registration_lock(dry_run: bool):
    if dry_run:
        yield
        return
    path = manifest_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _state(skill_dir: Path, name: str, owned: dict) -> str:
    target = skill_dir / "SKILL.md"
    if skill_dir.is_symlink() or target.is_symlink():
        return "conflict"
    if not skill_dir.exists():
        return "missing"
    record = owned.get(str(skill_dir))
    if not record or not skill_dir.is_dir():
        return "conflict"
    try:
        current = _hash(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return "conflict"
    if current not in {record.get("hash"), record.get("pending_hash")}:
        return "modified"
    return "current" if current == _hash(bundle_text(name)) else "outdated"


def _targets(runtime: str | None) -> list[dict]:
    """Unique physical roots for installed runtimes; aliases are reported, not duplicated."""
    groups: dict[str, dict] = {}
    for entry in runtime_roots():
        if runtime and entry["runtime"] != runtime:
            continue
        if not entry["available"]:
            continue
        group = groups.setdefault(entry["resolved_root"], {"resolved_root": entry["resolved_root"], "runtimes": [],
                                                           "roots": []})
        group["runtimes"].append(entry["runtime"])
        group["roots"].append(entry["root"])
    return list(groups.values())


def status(runtime: str | None = None) -> dict:
    owned = _ownership(_load_manifest())
    targets = []
    for group in _targets(runtime):
        root = Path(group["resolved_root"])
        group["skills"] = {name: _state(root / name, name, owned) for name in BUNDLE_NAMES}
        targets.append(group)
    from corral import __version__

    return {"bundle_version": __version__, "skills": list(BUNDLE_NAMES), "targets": targets,
            "runtimes": runtime_roots() if runtime is None else [r for r in runtime_roots() if r["runtime"] == runtime],
            "manifest": str(manifest_path()),
            "note": "File presence is not proof that a runtime loaded the skill; start a fresh session to verify."}


def _write_skill(skill_dir: Path, name: str) -> None:
    """Replace only the registered file; additional user files stay in place."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=skill_dir, prefix=".SKILL.corral-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(bundle_text(name))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, skill_dir / "SKILL.md")
    finally:
        Path(tmp).unlink(missing_ok=True)


def install(*, dry_run: bool, runtime: str | None = None) -> dict:
    with _registration_lock(dry_run):
        return _install(dry_run=dry_run, runtime=runtime)


def _install(*, dry_run: bool, runtime: str | None = None) -> dict:
    from corral import __version__

    manifest = _load_manifest()
    owned = manifest.setdefault("installed", {})
    pending = manifest.setdefault("pending", {})
    actions, conflicts = [], []
    for group in _targets(runtime):
        root = Path(group["resolved_root"])
        for name in BUNDLE_NAMES:
            skill_dir = root / name
            state = _state(skill_dir, name, _ownership(manifest))
            action = {"missing": "create", "outdated": "update", "current": "unchanged"}.get(state, "conflict")
            entry = {"skill": name, "path": str(skill_dir), "runtimes": group["runtimes"], "action": action,
                     "state": state}
            if action == "conflict":
                entry["hint"] = ("A same-name skill Corral did not write (or one edited since) exists; "
                                 "Corral leaves it untouched. Remove or rename it, then run corral skills install.")
                conflicts.append(entry)
            elif action != "unchanged" and not dry_run:
                pending[str(skill_dir)] = {"skill": name, "hash": _hash(bundle_text(name)), "at": time.time()}
                _save_manifest(manifest)
                _write_skill(skill_dir, name)
            if action in {"create", "update", "unchanged"} and not dry_run:
                owned[str(skill_dir)] = {"skill": name, "hash": _hash(bundle_text(name)), "version": __version__,
                                         "at": time.time()}
                pending.pop(str(skill_dir), None)
                _save_manifest(manifest)
            actions.append(entry)
    if not dry_run:
        manifest["bundle_version"] = __version__
        manifest["conflicts"] = conflicts
        manifest["targets"] = digest_roots()
        manifest["checked_at"] = time.time()
        _save_manifest(manifest)
    changed = any(a["action"] in {"create", "update"} for a in actions)
    return {"dry_run": dry_run, "status": "planned" if dry_run else ("applied" if changed else "unchanged"),
            "actions": actions, "conflicts": conflicts, "bundle_version": __version__}


def uninstall(*, dry_run: bool, runtime: str | None = None) -> dict:
    with _registration_lock(dry_run):
        return _uninstall(dry_run=dry_run, runtime=runtime)


def _uninstall(*, dry_run: bool, runtime: str | None = None) -> dict:
    manifest = _load_manifest()
    owned = manifest.get("installed", {})
    actions = []
    for path, record in sorted(owned.items()):
        skill_dir = Path(path)
        name = record.get("skill")
        if runtime and not any(runtime in g["runtimes"] and str(Path(g["resolved_root"]) / name) == path
                               for g in _targets(None)):
            continue
        state = _state(skill_dir, name, _ownership(manifest)) if name in BUNDLE_NAMES else "conflict"
        removable = state in {"current", "outdated"}
        actions.append({"skill": name, "path": path, "action": "remove" if removable else
                        ("forget" if state == "missing" else "keep_modified"), "state": state})
        if not dry_run and (removable or state == "missing"):
            if removable:
                (skill_dir / "SKILL.md").unlink()
                if not any(skill_dir.iterdir()):
                    skill_dir.rmdir()
            owned.pop(path, None)
            manifest.get("pending", {}).pop(path, None)
    if not dry_run:
        _save_manifest(manifest)
    return {"dry_run": dry_run, "actions": actions}


def digest_roots() -> str:
    return _hash(json.dumps(runtime_roots(), sort_keys=True))


def auto_register() -> None:
    """Best-effort bounded reconcile on interactive start; never prints to stdout."""
    if os.environ.get("CORRAL_SKILLS_AUTO") == "0":
        return
    try:
        from corral import __version__

        manifest = _load_manifest()
        if manifest.get("bundle_version") == __version__ and manifest.get("targets") == digest_roots():
            if not manifest.get("pending") and not manifest.get("conflicts"):
                return
            if time.time() - manifest.get("checked_at", 0) < 60:
                return
        install(dry_run=False)
    except Exception:  # noqa: BLE001 - registration must never block interactive startup
        return
