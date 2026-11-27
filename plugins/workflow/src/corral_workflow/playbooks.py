"""Playbook library discovery, versioned saving, optimization signals, and opt-in thin skill export.

Playbooks are ordinary Python workflow scripts stored in:
- project scope: ``<cwd>/.corral/workflows/<name>.py`` (higher precedence)
- global scope: ``<workflow_root>/playbooks/<name>.py``

Metadata is extracted purely via ``ast`` (never executing user code): module docstring
plus optional literal ``WORKFLOW_META = {"description": ..., "tags": [...], "args": {...}}``.
Thin pointer skills are strictly opt-in (``playbook export-skill``), never auto-generated
per run, and always gate execution behind ``--dry-run`` preview and explicit user authorization.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from corral_workflow.engine import compile_workflow_script
from corral_workflow.errors import EXIT_CONFLICT, EXIT_NOT_FOUND, EXIT_USAGE, WorkflowError
from corral_workflow.store import WorkflowStore, utc_iso

PLAYBOOK_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PROJECT_PLAYBOOKS_REL = Path(".corral") / "workflows"


def _validate_slug(name: str, *, label: str = "Playbook") -> str:
    cleaned = name.strip()
    if cleaned.endswith(".py"):
        cleaned = cleaned[:-3]
    if not PLAYBOOK_SLUG_RE.match(cleaned):
        raise WorkflowError(
            "usage_error",
            f"Invalid {label.lower()} name {name!r}: use lowercase letters, digits, '-' or '_' (max 64 chars).",
            EXIT_USAGE,
        )
    return cleaned


def playbook_version(source: str) -> str:
    """Deterministic 12-char SHA-256 optimistic-lock fingerprint over script UTF-8 bytes."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


def _file_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:24]


def _atomic_write_text(dest: Path, content: str, *, mode: int = 0o644) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.corral-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, dest)
    finally:
        Path(tmp).unlink(missing_ok=True)


def parse_playbook_metadata(source: str, *, filename: str = "<playbook>") -> dict[str, Any]:
    """Pure-AST extraction of docstring, optional ``WORKFLOW_META``, phases, and capabilities."""
    try:
        tree = ast.parse(source, filename=filename, mode="exec")
    except SyntaxError as exc:
        return {
            "description": "",
            "tags": [],
            "args": {},
            "phases_detected": [],
            "capabilities_referenced": [],
            "syntax_ok": False,
            "syntax_error": f"line {exc.lineno}: {exc.msg}",
        }

    doc = (ast.get_docstring(tree) or "").strip()
    doc_first_line = next((line.strip() for line in doc.splitlines() if line.strip()), "")

    meta: dict[str, Any] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "WORKFLOW_META":
                    try:
                        val = ast.literal_eval(stmt.value)
                        if isinstance(val, dict):
                            meta = val
                    except (ValueError, TypeError):
                        pass

    description = str(meta.get("description") or doc_first_line).strip()
    raw_tags = meta.get("tags")
    tags = (
        sorted({str(t).strip() for t in raw_tags if str(t).strip()})
        if isinstance(raw_tags, (list, tuple, set))
        else []
    )
    raw_args = meta.get("args")
    args_meta = {str(k): v for k, v in raw_args.items()} if isinstance(raw_args, dict) else {}

    phases: list[str] = []
    caps: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "phase" and node.args:
            arg0 = node.args[0]
            if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                phases.append(arg0.value)
        elif node.func.id in {"agent", "llm"}:
            for kw in node.keywords:
                if kw.arg in {"capability", "profile", "target"} and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str) and kw.value.value.strip():
                        caps.add(kw.value.value.strip())

    return {
        "description": description,
        "tags": tags,
        "args": args_meta,
        "phases_detected": phases,
        "capabilities_referenced": sorted(caps),
        "syntax_ok": True,
        "syntax_error": None,
    }


def _scope_dir(store: WorkflowStore, scope: str, cwd: Path) -> Path:
    if scope == "project":
        return cwd / PROJECT_PLAYBOOKS_REL
    if scope == "global":
        return store.root / "playbooks"
    raise WorkflowError(
        "usage_error",
        f"Unsupported playbook scope: {scope!r} (use 'project' or 'global').",
        EXIT_USAGE,
    )


def _exported_skill_for_playbook(
    store: WorkflowStore,
    name: str,
    *,
    scope: str,
    cwd: Path,
) -> dict[str, Any] | None:
    records = store.get_exported_skills()
    for skill_md, rec in records.items():
        if rec.get("playbook") != name:
            continue
        rec_scope = rec.get("scope")
        rec_cwd = rec.get("cwd")
        if rec_scope == "project" and rec_cwd and Path(rec_cwd).resolve() != cwd.resolve():
            continue
        if rec_scope and rec_scope != scope and scope in {"project", "global"}:
            # Prefer matching scope, but global export still applies to global playbooks
            if not (rec_scope == "project" and scope == "global"):
                continue
        path = Path(skill_md)
        return {
            "skill_name": rec.get("skill_name"),
            "scope": rec_scope,
            "path": skill_md,
            "exists": path.is_file(),
        }
    return None


def discover_playbooks(
    store: WorkflowStore,
    *,
    cwd: Path | str | None = None,
    scope: str = "all",
    tag: str | None = None,
) -> list[dict[str, Any]]:
    """List saved playbooks across project and global scopes with compact execution signals."""
    if scope not in {"all", "project", "global"}:
        raise WorkflowError("usage_error", f"Invalid scope: {scope!r} (use all, project, or global).", EXIT_USAGE)
    work_cwd = Path(cwd or os.getcwd()).expanduser().resolve()
    scopes = ["project", "global"] if scope == "all" else [scope]
    seen_names: set[str] = set()
    items: list[dict[str, Any]] = []

    for sc in scopes:
        directory = _scope_dir(store, sc, work_cwd)
        if not directory.is_dir():
            continue
        for py_file in sorted(directory.glob("*.py")):
            if not py_file.is_file() or py_file.name.startswith("_"):
                continue
            name = py_file.stem
            try:
                source = py_file.read_text(encoding="utf-8")
            except OSError:
                continue
            meta = parse_playbook_metadata(source, filename=str(py_file))
            if tag and tag not in meta["tags"]:
                continue
            signals = store.get_workflow_optimization_signals(name, script_path=str(py_file.resolve()))
            exported = _exported_skill_for_playbook(store, name, scope=sc, cwd=work_cwd)
            shadowed = name in seen_names
            seen_names.add(name)
            items.append(
                {
                    "name": name,
                    "scope": sc,
                    "path": str(py_file.resolve()),
                    "version": playbook_version(source),
                    "description": meta["description"],
                    "tags": meta["tags"],
                    "args": meta["args"],
                    "phases_detected": meta["phases_detected"],
                    "capabilities_referenced": meta["capabilities_referenced"],
                    "syntax_ok": meta["syntax_ok"],
                    "shadowed_by_project": shadowed,
                    "runs_count": signals["runs_analyzed"],
                    "success_rate": signals["success_rate"],
                    "avg_elapsed_ms": signals["avg_elapsed_ms"],
                    "exported_skill": exported,
                }
            )
    return items


def resolve_playbook_file(
    store: WorkflowStore,
    name: str,
    *,
    cwd: Path | str | None = None,
    scope: str | None = None,
) -> tuple[str, str, Path]:
    """Locate a playbook by name (project overrides global unless ``scope`` is explicit)."""
    slug = _validate_slug(name)
    work_cwd = Path(cwd or os.getcwd()).expanduser().resolve()
    scopes = [scope] if scope in {"project", "global"} else ["project", "global"]
    for sc in scopes:
        cand = _scope_dir(store, sc, work_cwd) / f"{slug}.py"
        if cand.is_file():
            return slug, sc, cand.resolve()
    raise WorkflowError(
        "not_found",
        f"Playbook '{slug}' not found in {', '.join(scopes)} scope(s).",
        EXIT_NOT_FOUND,
        hint="Run 'corral workflow playbook list' to see available playbooks, or save one with 'playbook save'.",
        context={"playbook": slug, "scopes_searched": scopes, "cwd": str(work_cwd)},
    )


def show_playbook(
    store: WorkflowStore,
    name: str,
    *,
    cwd: Path | str | None = None,
    scope: str | None = None,
    include_source: bool = True,
) -> dict[str, Any]:
    """Return playbook metadata, version, optional source, and historical optimization signals."""
    work_cwd = Path(cwd or os.getcwd()).expanduser().resolve()
    slug, found_scope, path = resolve_playbook_file(store, name, cwd=work_cwd, scope=scope)
    source = path.read_text(encoding="utf-8")
    meta = parse_playbook_metadata(source, filename=str(path))
    signals = store.get_workflow_optimization_signals(slug, script_path=str(path))
    exported = _exported_skill_for_playbook(store, slug, scope=found_scope, cwd=work_cwd)
    payload: dict[str, Any] = {
        "name": slug,
        "scope": found_scope,
        "path": str(path),
        "version": playbook_version(source),
        **meta,
        "optimization_signals": signals,
        "exported_skill": exported,
    }
    if include_source:
        payload["source"] = source
    return payload


def save_playbook(
    store: WorkflowStore,
    name: str,
    *,
    source: str,
    scope: str = "project",
    cwd: Path | str | None = None,
    expected_version: str | None = None,
    dry_run: bool = False,
    actor: str = "local-user",
) -> dict[str, Any]:
    """Declaratively create or update a playbook with syntax validation and ``--version`` lock."""
    slug = _validate_slug(name)
    work_cwd = Path(cwd or os.getcwd()).expanduser().resolve()
    dest = _scope_dir(store, scope, work_cwd) / f"{slug}.py"
    if dest.is_symlink():
        raise WorkflowError("conflict", f"Refusing to write through symlink: {dest}", EXIT_CONFLICT)

    # Syntax-check before writing
    _ = compile_workflow_script(source, filename=str(dest))
    new_ver = playbook_version(source)

    before_ver: str | None = None
    action = "created"
    if dest.is_file():
        before_source = dest.read_text(encoding="utf-8")
        before_ver = playbook_version(before_source)
        if not expected_version:
            raise WorkflowError(
                "confirmation_required",
                f"Overwriting existing playbook '{slug}' ({scope}) requires --version {before_ver}.",
                EXIT_USAGE,
                hint=f"Re-run with --version {before_ver}.",
                context={"playbook": slug, "scope": scope, "current_version": before_ver, "path": str(dest)},
            )
        if expected_version != before_ver:
            raise WorkflowError(
                "conflict",
                f"Version mismatch for playbook '{slug}': expected {expected_version}, current is {before_ver}.",
                EXIT_CONFLICT,
                context={"playbook": slug, "expected_version": expected_version, "current_version": before_ver},
            )
        action = "unchanged" if new_ver == before_ver else "updated"

    meta = parse_playbook_metadata(source, filename=str(dest))
    if not dry_run and action != "unchanged":
        _atomic_write_text(dest, source, mode=0o644)
        store.record_playbook_audit(
            actor=actor,
            action=f"playbook.{action}",
            target_id=f"{scope}:{slug}",
            before_version=before_ver,
            after_version=new_ver,
            detail={"path": str(dest.resolve()), "scope": scope},
        )
    return {
        "dry_run": dry_run,
        "status": action,
        "playbook": {
            "name": slug,
            "scope": scope,
            "path": str(dest.resolve()),
            "version": new_ver,
            "previous_version": before_ver,
            **meta,
        },
    }


def delete_playbook(
    store: WorkflowStore,
    name: str,
    *,
    scope: str | None = None,
    cwd: Path | str | None = None,
    expected_version: str | None = None,
    dry_run: bool = False,
    actor: str = "local-user",
) -> dict[str, Any]:
    """Delete a saved playbook with ``--version`` optimistic lock."""
    work_cwd = Path(cwd or os.getcwd()).expanduser().resolve()
    slug, found_scope, path = resolve_playbook_file(store, name, cwd=work_cwd, scope=scope)
    current_ver = playbook_version(path.read_text(encoding="utf-8"))
    if not expected_version:
        raise WorkflowError(
            "confirmation_required",
            f"Deleting playbook '{slug}' ({found_scope}) requires --version {current_ver}.",
            EXIT_USAGE,
            context={"playbook": slug, "scope": found_scope, "current_version": current_ver},
        )
    if expected_version != current_ver:
        raise WorkflowError(
            "conflict",
            f"Version mismatch for playbook '{slug}': expected {expected_version}, current is {current_ver}.",
            EXIT_CONFLICT,
            context={"playbook": slug, "expected_version": expected_version, "current_version": current_ver},
        )
    if not dry_run:
        path.unlink()
        store.record_playbook_audit(
            actor=actor,
            action="playbook.delete",
            target_id=f"{found_scope}:{slug}",
            before_version=current_ver,
            detail={"path": str(path), "scope": found_scope},
        )
    return {
        "dry_run": dry_run,
        "deleted": slug,
        "scope": found_scope,
        "path": str(path),
        "version": current_ver,
    }


def _resolve_export_skill_dir(
    *,
    skill_name: str,
    scope: str,
    dest: Path | str | None,
    cwd: Path,
) -> Path:
    if dest is not None:
        return Path(dest).expanduser().resolve() / skill_name
    if scope == "project":
        return cwd / ".agents" / "skills" / skill_name
    if scope == "global":
        shared = Path.home() / ".agents" / "skills"
        root = Path(os.path.realpath(shared)) if shared.exists() else shared
        return root / skill_name
    raise WorkflowError("usage_error", f"Invalid skill export scope: {scope!r} (use project or global).", EXIT_USAGE)


def render_thin_skill_markdown(
    playbook: dict[str, Any],
    *,
    skill_name: str,
    corral_bin: str = "corral",
) -> str:
    """Render a minimal (~16-line) pointer SKILL.md that never duplicates workflow source."""
    name = playbook["name"]
    desc = playbook.get("description") or f"Run the Corral workflow playbook '{name}'."
    desc_clean = " ".join(desc.splitlines()).strip().rstrip(".")
    args_meta = playbook.get("args") or {}
    args_hint = " ".join(f"--arg {k}=<val>" for k in list(args_meta.keys())[:4])
    run_args = f" {args_hint}" if args_hint else ""
    lines = [
        "---",
        f"name: {skill_name}",
        (
            f"description: {desc_clean}. Triggers the Corral workflow playbook `{name}`; "
            "always preview with `--dry-run` first and wait for explicit user authorization before executing workers."
        ),
        "---",
        "",
        f"# Workflow Playbook: `{name}`",
        "",
        f"Thin pointer to Corral playbook `{name}` (never duplicate script logic here).",
        "",
        "1. Inspect latest script, arguments, and historical optimization signals:",
        f"   `{corral_bin} workflow playbook show {name}`",
        "2. Preview read-only execution plan (zero worker execution, zero circuit mutation):",
        f"   `{corral_bin} workflow run {name}{run_args} --dry-run`",
        "3. Wait for explicit user authorization before executing workers, then run:",
        f"   `{corral_bin} workflow run {name}{run_args}`",
        "",
    ]
    return "\n".join(lines)


def export_playbook_skill(
    store: WorkflowStore,
    name: str,
    *,
    scope: str = "project",
    dest: Path | str | None = None,
    cwd: Path | str | None = None,
    skill_name: str | None = None,
    dry_run: bool = False,
    actor: str = "local-user",
    corral_bin: str = "corral",
) -> dict[str, Any]:
    """Opt-in export of a thin pointer SKILL.md for a mature, high-frequency playbook."""
    work_cwd = Path(cwd or os.getcwd()).expanduser().resolve()
    playbook = show_playbook(store, name, cwd=work_cwd, include_source=False)
    slug = playbook["name"]
    resolved_skill_name = _validate_slug(skill_name or f"corral-wf-{slug}", label="Skill")
    skill_dir = _resolve_export_skill_dir(skill_name=resolved_skill_name, scope=scope, dest=dest, cwd=work_cwd)
    skill_md = skill_dir / "SKILL.md"
    key = str(skill_md.resolve()) if skill_md.exists() else str(skill_md)

    if skill_dir.is_symlink() or skill_md.is_symlink():
        raise WorkflowError("conflict", f"Refusing to write exported skill through symlink: {skill_dir}", EXIT_CONFLICT)

    owned = store.get_exported_skills()
    existing_rec = owned.get(key)
    action = "create"
    if skill_md.exists():
        if not existing_rec:
            raise WorkflowError(
                "conflict",
                f"Skill file already exists and is not Corral-owned: {skill_md}",
                EXIT_CONFLICT,
                hint="Choose a different --skill-name / --dest or remove the existing file manually.",
                context={"path": str(skill_md)},
            )
        current_hash = _file_sha256(skill_md.read_text(encoding="utf-8"))
        if current_hash != existing_rec.get("hash"):
            raise WorkflowError(
                "conflict",
                f"Exported skill was manually modified since Corral wrote it: {skill_md}",
                EXIT_CONFLICT,
                hint="Inspect or remove your local edits before re-exporting.",
                context={"path": str(skill_md)},
            )
        action = "update"

    content = render_thin_skill_markdown(playbook, skill_name=resolved_skill_name, corral_bin=corral_bin)
    new_hash = _file_sha256(content)
    if existing_rec and existing_rec.get("hash") == new_hash:
        action = "unchanged"

    if not dry_run and action != "unchanged":
        _atomic_write_text(skill_md, content, mode=0o644)
        store.record_exported_skill(
            str(skill_md.resolve()),
            {
                "skill_name": resolved_skill_name,
                "playbook": slug,
                "scope": scope,
                "cwd": str(work_cwd) if scope == "project" else None,
                "hash": new_hash,
                "updated_at": time.time(),
                "updated_at_utc": utc_iso(),
            },
            actor=actor,
        )
    return {
        "dry_run": dry_run,
        "action": action,
        "playbook": slug,
        "skill_name": resolved_skill_name,
        "scope": scope,
        "path": str(skill_md),
        "hash": new_hash,
    }


def unexport_playbook_skill(
    store: WorkflowStore,
    name: str,
    *,
    scope: str = "project",
    dest: Path | str | None = None,
    cwd: Path | str | None = None,
    skill_name: str | None = None,
    dry_run: bool = False,
    actor: str = "local-user",
) -> dict[str, Any]:
    """Safely remove a Corral-exported thin pointer SKILL.md (refusing modified files)."""
    work_cwd = Path(cwd or os.getcwd()).expanduser().resolve()
    slug = _validate_slug(name)
    resolved_skill_name = _validate_slug(skill_name or f"corral-wf-{slug}", label="Skill")
    skill_dir = _resolve_export_skill_dir(skill_name=resolved_skill_name, scope=scope, dest=dest, cwd=work_cwd)
    skill_md = skill_dir / "SKILL.md"
    key = str(skill_md.resolve()) if skill_md.exists() else str(skill_md)

    owned = store.get_exported_skills()
    existing_rec = owned.get(key)
    if not existing_rec:
        raise WorkflowError(
            "not_found",
            f"No Corral-exported skill record found at {skill_md}.",
            EXIT_NOT_FOUND,
            context={"path": str(skill_md), "skill_name": resolved_skill_name},
        )
    if skill_md.is_symlink():
        raise WorkflowError("conflict", f"Refusing to delete symlink: {skill_md}", EXIT_CONFLICT)
    if skill_md.is_file():
        current_hash = _file_sha256(skill_md.read_text(encoding="utf-8"))
        if current_hash != existing_rec.get("hash"):
            raise WorkflowError(
                "conflict",
                f"Exported skill was manually modified; refusing automatic deletion: {skill_md}",
                EXIT_CONFLICT,
                context={"path": str(skill_md)},
            )
    if not dry_run:
        skill_md.unlink(missing_ok=True)
        if skill_dir.is_dir() and not any(skill_dir.iterdir()):
            skill_dir.rmdir()
        store.remove_exported_skill(key, actor=actor)
    return {
        "dry_run": dry_run,
        "removed": True,
        "playbook": slug,
        "skill_name": resolved_skill_name,
        "scope": scope,
        "path": str(skill_md),
    }
