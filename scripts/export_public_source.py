#!/usr/bin/env python3
"""Sanitized public-source exporter.

Builds a publishable copy of one committed source snapshot without its
private Git history. Reads every file through ``git ls-tree`` / ``git
cat-file`` from the selected commit, so untracked files, working-tree
edits and the ``.git`` directory itself can never leak into the output.

Dependency-free maintainer tool: only the standard library plus an
argv-driven native ``git`` subprocess. It never initializes a Git
repository, never publishes, and never touches the source repository.

Recipe, receipt and checker inputs are the maintainer's private working
material: keep them outside the public repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TOOL = "export_public_source"
RECEIPT_VERSION = 1

MAX_FILES = 20000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_RECIPE_BYTES = 1024 * 1024
MAX_FORBIDDEN_ENTRIES = 1000
MAX_FORBIDDEN_LENGTH = 4096

# Non-placeholder macOS personal paths are rejected by the privacy
# preflight. Segments in this set (case-insensitive) or starting with "<"
# are treated as documentation placeholders, not personal data.
PLACEHOLDER_USER_SEGMENTS = frozenset({"example", "shared", "yourname", "username"})
_PERSONAL_PATH_RE = re.compile(rb"/Users/([^\s/\x00]+)")

TRANSFORM_KIND_REPLACEMENT_FILE = "replacement_file"
TRANSFORM_KIND_REPLACEMENTS = "replacements"
TRANSFORM_KIND_REMOVE_LINES = "remove_lines"


class Failure(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 1):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def fail_usage(message: str) -> Failure:
    return Failure("usage_error", message, 2)


def fail_export(message: str) -> Failure:
    return Failure("export_error", message, 1)


def run_git(source: str, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", source, *args],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise fail_export(f"cannot execute git: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise fail_export(f"git {' '.join(args[:3])} failed: {detail[0] if detail else 'unknown error'}")
    return result.stdout


def check_rel_path(value: str, role: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or "\x00" in value:
        raise fail_usage(f"recipe {role} must be a relative path, got {value!r}")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise fail_usage(f"recipe {role} must not contain empty, '.' or '..' segments, got {value!r}")
    if value == ".git" or value.startswith(".git/"):
        raise fail_usage(f"recipe {role} must not reference .git, got {value!r}")
    return value


def normalize_ui_path(path: str) -> str:
    if path == "apple" or path.startswith("apple/"):
        return path[len("apple/") :] if path.startswith("apple/") else ""
    return path


def is_ui_path(path: str) -> bool:
    """Public byte-identical paths: SwiftUI views, client shells, resources.

    Only Shared/UI, Shared/Resources and the iOS/macOS Swift shells are
    copy-only. Shared/Core stays transformable so an explicit recipe can
    redact non-UI details (for example a hardcoded team access group) while
    every other covered file keeps byte equality with the commit.
    """
    candidate = normalize_ui_path(path)
    if candidate == "Shared/UI" or candidate.startswith("Shared/UI/"):
        return True
    if candidate == "Shared/Resources" or candidate.startswith("Shared/Resources/"):
        return True
    if candidate.endswith(".swift") and (
        candidate.startswith("iOS/") or candidate.startswith("macOS/") or "/UI/" in candidate
    ):
        return True
    return False


def load_recipe(recipe_path: str | None) -> tuple[dict, str | None]:
    if recipe_path is None:
        return {}, None
    raw_path = Path(recipe_path)
    if not raw_path.is_file():
        raise fail_usage(f"recipe file not found: {recipe_path}")
    if raw_path.stat().st_size > MAX_RECIPE_BYTES:
        raise fail_usage("recipe file exceeds size bound")
    try:
        recipe = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise fail_usage(f"recipe file is not valid JSON: {exc}") from exc
    if not isinstance(recipe, dict):
        raise fail_usage("recipe root must be a JSON object")
    return recipe, str(raw_path.parent)


def load_forbidden_file(path: str) -> list[str]:
    raw_path = Path(path)
    if not raw_path.is_file():
        raise fail_usage(f"forbidden-list file not found: {path}")
    if raw_path.stat().st_size > MAX_RECIPE_BYTES:
        raise fail_usage("forbidden-list file exceeds size bound")
    try:
        text = raw_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise fail_usage(f"cannot read forbidden-list file: {exc}") from exc
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        entries = payload.get("forbidden_strings", [])
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    if not isinstance(entries, list) or any(not isinstance(item, str) for item in entries):
        raise fail_usage("forbidden-list must be a JSON object, JSON array, or plain text lines")
    return entries


def check_forbidden_entries(entries: list[str], origin: str) -> list[str]:
    if len(entries) > MAX_FORBIDDEN_ENTRIES:
        raise fail_usage(f"{origin} exceeds entry bound")
    cleaned: list[str] = []
    for entry in entries:
        if not entry:
            raise fail_usage(f"{origin} contains an empty string")
        if len(entry) > MAX_FORBIDDEN_LENGTH:
            raise fail_usage(f"{origin} contains an overlong string")
        cleaned.append(entry)
    return cleaned


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="export_public_source.py",
        description="Export one committed snapshot as sanitized public source without Git history.",
    )
    parser.add_argument("--source", required=True, help="Absolute path of the source Git checkout.")
    parser.add_argument("--commit", default="HEAD", help="Exact commit to export (default HEAD).")
    parser.add_argument("--output", required=True, help="Absolute output directory; must not exist.")
    parser.add_argument("--recipe", default=None, help="Optional private recipe JSON file.")
    parser.add_argument(
        "--forbidden-list",
        default=None,
        help="Optional checker input: JSON object/array or plain-text forbidden strings.",
    )
    parser.add_argument("--receipt", default=None, help="Optional file path for the JSON receipt.")
    args = parser.parse_args(argv)
    for name in ("source", "output"):
        value = getattr(args, name)
        if not os.path.isabs(value):
            raise fail_usage(f"--{name} must be an absolute path, got {value!r}")
    return args


def resolve_commit(source: str, requested: str) -> str:
    if not os.path.isdir(source):
        raise fail_usage(f"source directory not found: {source}")
    run_git(source, "rev-parse", "--git-dir")
    output = run_git(source, "rev-parse", "--verify", "--quiet", f"{requested}^{{commit}}")
    commit = output.decode("ascii", "replace").strip()
    if not commit:
        raise fail_usage(f"unknown commit: {requested}")
    return commit


def read_tree(source: str, commit: str) -> list[dict]:
    output = run_git(source, "ls-tree", "-r", "-z", "--full-tree", commit)
    records: list[dict] = []
    for record in output.split(b"\x00"):
        if not record:
            continue
        try:
            meta, raw_path = record.split(b"\t", 1)
            mode, kind, sha = meta.decode("ascii").split(" ")
            path = raw_path.decode("utf-8")
        except ValueError as exc:
            raise fail_export("cannot parse git ls-tree output") from exc
        records.append({"mode": mode, "kind": kind, "sha": sha, "path": path})
    return records


def validate_tree(records: list[dict]) -> None:
    if len(records) > MAX_FILES:
        raise fail_export(f"tree exceeds file bound ({MAX_FILES})")
    seen: set[str] = set()
    for record in records:
        path = record["path"]
        check_rel_path(path, "tree path")
        if path in seen:
            raise fail_export(f"duplicate tree path: {path}")
        seen.add(path)
        if record["kind"] == "commit":
            raise fail_export(f"submodule found at {path}; nested repositories are rejected")
        if path == ".gitmodules":
            raise fail_export(".gitmodules present; nested repositories are rejected")
        if record["mode"] == "120000" or record["kind"] == "link":
            raise fail_export(f"symlink found at {path}; symlinks are rejected")
        if record["kind"] != "blob":
            raise fail_export(f"unsupported tree entry at {path}")


def read_blob(source: str, sha: str, path: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", source, "cat-file", "-p", sha],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise fail_export(f"cannot execute git: {exc}") from exc
    if result.returncode != 0:
        raise fail_export(f"cannot read blob for {path}")
    return result.stdout


def load_transforms(
    recipe: dict, tree_paths: set[str], recipe_dir: str | None
) -> tuple[set[str], dict[str, dict], list[str]]:
    raw_exclude = recipe.get("exclude", [])
    raw_replace = recipe.get("replace", {})
    if not isinstance(raw_exclude, list) or any(not isinstance(item, str) for item in raw_exclude):
        raise fail_usage("recipe 'exclude' must be an array of paths")
    if not isinstance(raw_replace, dict):
        raise fail_usage("recipe 'replace' must be an object keyed by path")
    exclusions: set[str] = set()
    for entry in raw_exclude:
        path = check_rel_path(entry, "exclusion")
        if path not in tree_paths:
            raise fail_usage(f"exclusion targets unknown tree path: {path}")
        exclusions.add(path)
    transforms: dict[str, dict] = {}
    for target, spec in raw_replace.items():
        path = check_rel_path(target, "replacement target")
        if path not in tree_paths:
            raise fail_usage(f"replacement targets unknown tree path: {path}")
        if path in exclusions:
            raise fail_usage(f"replacement targets excluded path: {path}")
        if is_ui_path(path):
            raise fail_usage(f"replacement targets UI source, which is copy-only: {path}")
        if not isinstance(spec, dict):
            raise fail_usage(f"replacement for {path} must be an object")
        allowed = {"replacements", "remove_lines_containing", "replacement_file"}
        unknown = set(spec) - allowed
        if unknown:
            raise fail_usage(f"replacement for {path} has unknown keys: {sorted(unknown)}")
        replacements = spec.get("replacements", [])
        removals = spec.get("remove_lines_containing", [])
        replacement_file = spec.get("replacement_file")
        if not isinstance(replacements, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("old"), str) or not isinstance(item.get("new"), str)
            for item in replacements
        ):
            raise fail_usage(f"replacement for {path} needs 'replacements' as [{{old, new}}]")
        if any(not item["old"] for item in replacements):
            raise fail_usage(f"replacement for {path} contains an empty 'old' string")
        if not isinstance(removals, list) or any(not isinstance(item, str) or not item for item in removals):
            raise fail_usage(f"replacement for {path} needs 'remove_lines_containing' as non-empty strings")
        if replacement_file is not None:
            if not isinstance(replacement_file, str):
                raise fail_usage(f"replacement file for {path} must be a path string")
            if replacements or removals:
                raise fail_usage(f"replacement file for {path} cannot combine with other edits")
            if recipe_dir is None:
                raise fail_usage(f"replacement file for {path} needs --recipe for a base directory")
            candidate = Path(recipe_dir, replacement_file)
            if not candidate.is_file():
                raise fail_usage(f"replacement file for {path} not found")
            if candidate.stat().st_size > MAX_FILE_BYTES:
                raise fail_usage(f"replacement file for {path} exceeds size bound")
            try:
                content = candidate.read_bytes()
            except OSError as exc:
                raise fail_usage(f"cannot read replacement file for {path}: {exc}") from exc
            transforms[path] = {"replacement_file": content}
        else:
            if not replacements and not removals:
                raise fail_usage(f"replacement for {path} is empty")
            transforms[path] = {"replacements": replacements, "remove_lines": removals}
    for key in ("forbidden_strings",):
        entries = recipe.get(key, [])
        if not isinstance(entries, list) or any(not isinstance(item, str) for item in entries):
            raise fail_usage(f"recipe '{key}' must be an array of strings")
    return (
        exclusions,
        transforms,
        check_forbidden_entries(recipe.get("forbidden_strings", []), "recipe forbidden_strings"),
    )


def apply_transforms(data: bytes, path: str, spec: dict) -> tuple[bytes, list[str]]:
    if TRANSFORM_KIND_REPLACEMENT_FILE in spec:
        return bytes(spec[TRANSFORM_KIND_REPLACEMENT_FILE]), [TRANSFORM_KIND_REPLACEMENT_FILE]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise fail_export(f"replacement targets non-text file: {path}") from exc
    kinds: list[str] = []
    if spec.get("replacements"):
        for item in spec["replacements"]:
            if item["old"] not in text:
                raise fail_export(f"replacement matched nothing in {path}")
            text = text.replace(item["old"], item["new"])
        kinds.append(TRANSFORM_KIND_REPLACEMENTS)
    if spec.get("remove_lines"):
        lines = text.split("\n")
        kept = [line for line in lines if not any(needle in line for needle in spec["remove_lines"])]
        text = "\n".join(kept)
        kinds.append(TRANSFORM_KIND_REMOVE_LINES)
    return text.encode("utf-8"), kinds


def preflight(outputs: list[dict], private_values: list[bytes], forbidden: list[bytes]) -> None:
    needles = [(value, "private_value") for value in private_values if value]
    needles += [(value, "forbidden_string") for value in forbidden if value]
    for entry in outputs:
        data: bytes = entry["data"]
        for match in _PERSONAL_PATH_RE.finditer(data):
            segment = match.group(1).decode("ascii", "replace")
            lowered = segment.lower()
            if lowered.startswith("<") or lowered in PLACEHOLDER_USER_SEGMENTS:
                continue
            raise fail_export(f"privacy preflight: personal path in {entry['path']}")
        for value, kind in needles:
            if value in data:
                raise fail_export(f"privacy preflight: {kind} found in {entry['path']}")


def within_output(output: str, path: str) -> str:
    dest = os.path.normpath(os.path.join(output, path))
    root = os.path.normpath(output)
    if dest != root and not dest.startswith(root + os.sep):
        raise fail_export(f"path escapes output directory: {path}")
    return dest


def write_outputs(output: str, entries: list[dict]) -> list[str]:
    written: list[str] = []
    try:
        os.makedirs(output, exist_ok=False)
        for entry in entries:
            dest = within_output(output, entry["path"])
            parent = os.path.dirname(dest)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(dest, "wb") as stream:
                stream.write(entry["data"])
            os.chmod(dest, 0o755 if entry["executable"] else 0o644)
            written.append(dest)
    except Failure:
        raise
    except (OSError, ValueError) as exc:
        raise fail_export(f"cannot write output: {exc}") from exc
    return written


def cleanup_owned(output: str) -> None:
    shutil.rmtree(output, ignore_errors=True)


def build_receipt(
    source: str,
    requested: str,
    commit: str,
    output: str,
    entries: list[dict],
    exclusions: set[str],
    applied: dict[str, list[str]],
) -> dict:
    files = []
    total = 0
    for entry in sorted(entries, key=lambda item: item["path"]):
        digest = hashlib.sha256(entry["data"]).hexdigest()
        total += len(entry["data"])
        files.append(
            {
                "path": entry["path"],
                "sha256": digest,
                "mode": "100755" if entry["executable"] else "100644",
                "bytes": len(entry["data"]),
                "transformed": entry["path"] in applied,
            }
        )
    return {
        "ok": True,
        "tool": TOOL,
        "receipt_version": RECEIPT_VERSION,
        "source": source,
        "requested_commit": requested,
        "commit": commit,
        "output": output,
        "file_count": len(files),
        "total_bytes": total,
        "git_history": "excluded",
        "untracked": "excluded",
        "exclusions": sorted(exclusions),
        "transforms": {path: applied[path] for path in sorted(applied)},
        "files": files,
        "preflight": {
            "checked_files": len(files),
            "checks": ["personal_paths", "private_values", "forbidden_strings"],
        },
    }


def export_source(args: argparse.Namespace) -> dict:
    source = os.path.normpath(args.source)
    output = os.path.normpath(args.output)
    if os.path.lexists(output):
        raise fail_usage(f"output already exists, refusing to clobber: {output}")
    parent = os.path.dirname(output)
    if parent and not os.path.isdir(parent):
        raise fail_usage(f"output parent directory not found: {parent}")
    source_real = os.path.realpath(source)
    output_real = os.path.realpath(output)
    if output_real == source_real or output_real.startswith(source_real + os.sep):
        raise fail_usage("output must not live inside the source checkout")

    recipe, recipe_dir = load_recipe(args.recipe)
    checker_entries = load_forbidden_file(args.forbidden_list) if args.forbidden_list else []
    checker_entries = check_forbidden_entries(checker_entries, "forbidden-list")

    commit = resolve_commit(source, args.commit)

    # Phase 1: read-only inspection of the committed tree.
    records = read_tree(source, commit)
    validate_tree(records)
    tree_paths = {record["path"] for record in records}
    exclusions, transforms, recipe_forbidden = load_transforms(recipe, tree_paths, recipe_dir)
    forbidden = recipe_forbidden + checker_entries

    # Phase 2: materialize every output file in memory, then preflight.
    planned: list[dict] = []
    total_bytes = 0
    private_values: list[bytes] = []
    for spec in transforms.values():
        for item in spec.get("replacements", []):
            private_values.append(item["old"].encode("utf-8"))
        for needle in spec.get("remove_lines", []):
            private_values.append(needle.encode("utf-8"))
    for record in records:
        path = record["path"]
        if path in exclusions:
            continue
        data = read_blob(source, record["sha"], path)
        if len(data) > MAX_FILE_BYTES:
            raise fail_export(f"file exceeds size bound: {path}")
        kinds: list[str] = []
        if path in transforms:
            data, kinds = apply_transforms(data, path, transforms[path])
        planned.append(
            {
                "path": path,
                "data": data,
                "executable": record["mode"] == "100755",
                "kinds": kinds,
            }
        )
        total_bytes += len(data)
    if len(planned) > MAX_FILES or total_bytes > MAX_TOTAL_BYTES:
        raise fail_export("planned output exceeds size bound")
    preflight(planned, private_values, [item.encode("utf-8") for item in forbidden])

    # Phase 3: the only mutating step; the output directory is entirely
    # owned by this invocation because pre-existence was refused above.
    applied = {entry["path"]: entry["kinds"] for entry in planned if entry["kinds"]}
    try:
        write_outputs(output, planned)
    except Exception:
        cleanup_owned(output)
        raise
    try:
        preflight_cleanup_check(output, planned)
    except Exception:
        cleanup_owned(output)
        raise
    return build_receipt(source, args.commit, commit, output, planned, exclusions, applied)


def preflight_cleanup_check(output: str, planned: list[dict]) -> None:
    actual: set[str] = set()
    for root, _, files in os.walk(output):
        for name in files:
            full = os.path.join(root, name)
            actual.add(os.path.relpath(full, output))
    expected = {entry["path"] for entry in planned}
    if actual != expected:
        raise fail_export("output contents differ from planned files")


def emit_receipt(receipt: dict, receipt_path: str | None) -> None:
    text = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True)
    if receipt_path:
        dest = Path(receipt_path)
        if dest.parent and not dest.parent.is_dir():
            raise fail_usage(f"receipt parent directory not found: {dest.parent}")
        fd, name = tempfile.mkstemp(dir=dest.parent or ".", prefix=".receipt-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, dest)
        except (OSError, ValueError) as exc:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise fail_export(f"cannot write receipt: {exc}") from exc
    print(text)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except Failure as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message}}), flush=True)
        print(f"{TOOL}: {exc.message}", file=sys.stderr)
        return exc.exit_code
    try:
        receipt = export_source(args)
    except Failure as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message}}), flush=True)
        print(f"{TOOL}: {exc.message}", file=sys.stderr)
        return exc.exit_code
    try:
        emit_receipt(receipt, args.receipt)
    except Failure as exc:
        cleanup_owned(os.path.normpath(args.output))
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message}}), flush=True)
        print(f"{TOOL}: {exc.message}", file=sys.stderr)
        return exc.exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
