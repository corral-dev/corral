#!/usr/bin/env python3
"""Pinned agent-session-control install source shared by CI, install and dev-env.

Mirrors the authority pattern of ``scripts/sesskit_dep.py``: this module owns
the pin (VERSION / artifact URL / digest); ``pyproject.toml`` and the install
paths mirror the generated fields verbatim once the coordinator publishes the
versioned immutable artifact.

Status: the upstream artifact is NOT yet published. There is no downloadable
wheel URL or digest to pin, so every helper that would emit one fails loudly
instead of inventing an artifact. The coordinator publishes the artifact, then
fills in WHEEL_URL / WHEEL_SHA256 below (and nothing else needs a code
change: dev-env, install.sh and verify_clean_install.py already branch on
this pin). A relative path / PYTHONPATH / editable install is development
only and never the final consumer dependency.

FINAL-PIN HOOK (for the coordinator, after publication):
  1. Set WHEEL_URL / WHEEL_SHA256 below (keep VERSION in sync).
  2. Add to pyproject ``dependencies``: ``"agent-session-control>=0.1.0"``,
     plus ``[tool.uv.sources]`` block from ``uv-source-table``; ``uv lock``.
  3. install.sh / verify_clean_install.py pick the pin up automatically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request

# --- single source of truth -------------------------------------------------
VERSION = "0.1.0"
PACKAGE = "agent-session-control"
# No published artifact yet (private source repo only). The coordinator
# publishes the versioned wheel, then fills URL + sha256 here.
WHEEL_URL: str | None = None
WHEEL_SHA256: str | None = None
REQUIRES = f"{PACKAGE}>={VERSION}"


def published() -> bool:
    """True once the coordinator has published a versioned immutable artifact."""
    return bool(WHEEL_URL and WHEEL_SHA256)


def status() -> dict:
    return {
        "package": PACKAGE,
        "version": VERSION,
        "published": published(),
        "wheel_url": WHEEL_URL,
        "wheel_sha256": WHEEL_SHA256,
        "requires": REQUIRES,
    }


def wheel_requirement() -> str:
    """PEP 508 direct reference with digest for pip. Fails until published."""
    if not published():
        raise RuntimeError(
            "agent-session-control 0.1.0 has no published artifact yet; "
            "the coordinator publishes the versioned wheel first (see module docstring). "
            "Development may install the local test wheel (see test-wheel-install), but the "
            "final Corral dependency must be a versioned immutable artifact."
        )
    assert WHEEL_URL is not None and WHEEL_SHA256 is not None
    return f"{PACKAGE} @ {WHEEL_URL}#sha256={WHEEL_SHA256}"


def uv_source_table() -> str:
    """``[tool.uv.sources]`` block derived from this pin for Corral's pyproject.

    Canonical direction: this module owns the pin; ``pyproject.toml`` mirrors
    the generated block verbatim. Fails until the artifact is published so no
    placeholder URL ever lands in the manifest.
    """
    if not published():
        raise RuntimeError("no published agent-session-control artifact to derive a uv source from yet")
    assert WHEEL_URL is not None
    return f'[tool.uv.sources]\n{PACKAGE} = {{ url = "{WHEEL_URL}" }}\n'


def test_wheel_path(library_root: str | None = None) -> str:
    """Newest locally built wheel for DEV TESTING ONLY (never manifests/pins).

    ``library_root`` defaults to the sibling ``AgentSessionControl`` checkout
    next to the Corral working tree; pass it explicitly when the layout
    differs. Raises when no built wheel exists (build one with
    ``python -m build`` in the library checkout first).
    """
    from pathlib import Path

    if library_root is None:
        here = Path(__file__).resolve()  # <corral>/cli/scripts/control_dep.py
        library_root = str(here.parents[3] / "AgentSessionControl")
    candidates = sorted(Path(library_root, "dist").glob(f"{PACKAGE.replace('-', '_')}-*.whl"))
    if not candidates:
        raise RuntimeError(
            f"no local {PACKAGE} wheel under {library_root}/dist; "
            "build one with `python -m build` in the library checkout first. "
            "This path is dev-testing only and must never enter manifests or pins."
        )
    return str(candidates[-1])


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_published_digests() -> None:
    """Fail loudly if the Release asset no longer matches the pinned digest."""
    if not published():
        raise RuntimeError("no published agent-session-control artifact to verify yet")
    assert WHEEL_URL is not None and WHEEL_SHA256 is not None
    request = urllib.request.Request(WHEEL_URL, headers={"User-Agent": "corral-control-dep"})
    last_err: Exception | None = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                digest = hashlib.sha256(response.read()).hexdigest()
            break
        except Exception as exc:  # noqa: BLE001 - retry transient CDN/TLS blips
            last_err = exc
    else:
        raise RuntimeError(f"download failed: {WHEEL_URL} ({last_err})")
    if digest != WHEEL_SHA256:
        raise RuntimeError(f"wheel sha256 mismatch: got {digest}, want {WHEEL_SHA256}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="agent-session-control dependency pin")
    parser.add_argument(
        "field",
        nargs="?",
        choices=["status", "json", "wheel-requirement", "requirement", "uv-source-table", "verify", "test-wheel"],
        default="status",
        help="Which pin field to print (default: status)",
    )
    parser.add_argument("--library-root", default=None, help="Library checkout root (test-wheel only)")
    args = parser.parse_args(argv)
    if args.field == "status":
        info = status()
        print(
            f"package={info['package']} version={info['version']} "
            f"published={info['published']} url={info['wheel_url']} sha256={info['wheel_sha256']}"
        )
        return 0 if info["published"] else 3
    if args.field == "json":
        print(json.dumps(status()))
        return 0
    if args.field in ("wheel-requirement", "requirement"):
        print(wheel_requirement())
        return 0
    if args.field == "uv-source-table":
        print(uv_source_table(), end="")
        return 0
    if args.field == "verify":
        verify_published_digests()
        print("digests ok")
        return 0
    print(test_wheel_path(args.library_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
