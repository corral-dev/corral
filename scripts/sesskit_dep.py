#!/usr/bin/env python3
"""Pinned SessKit install source shared by CI, install.sh, and Homebrew.

SessKit is not on PyPI yet. Corral's pyproject keeps a plain version pin
(``sesskit>=…``) so Homebrew offline installs stay valid; every pip path that
cannot see a local editable checkout must install this GitHub Release artifact
first (or alongside Corral).

Bump VERSION / URLs / digests together when SessKit publishes a new release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request

# --- single source of truth -------------------------------------------------
VERSION = "0.2.3"
TAG = f"v{VERSION}"
REPO = "x0c/sesskit"
WHEEL_NAME = f"sesskit-{VERSION}-py3-none-any.whl"
SDIST_NAME = f"sesskit-{VERSION}.tar.gz"
WHEEL_URL = f"https://github.com/{REPO}/releases/download/{TAG}/{WHEEL_NAME}"
SDIST_URL = f"https://github.com/{REPO}/releases/download/{TAG}/{SDIST_NAME}"
# sha256 of the published assets (verified 2026-09-30 against the GitHub Release).
WHEEL_SHA256 = "ba388b1d91f8de8cf6c7d5e59c809cd651f7e7b2e59d0ffbe5513927427b7dfd"
SDIST_SHA256 = "83b675e4bbdacb46da29439c25f1e28bfda66929cf73bede733433df15c2656d"
# Minimum version declared in Corral's pyproject.toml dependencies.
REQUIRES = f"sesskit>={VERSION}"


def wheel_requirement() -> str:
    """PEP 508 direct reference with digest for pip."""
    return f"sesskit @ {WHEEL_URL}#sha256={WHEEL_SHA256}"


def homebrew_resource_block() -> str:
    """Ruby ``resource "sesskit"`` block for the Homebrew formula generator."""
    return (
        '  resource "sesskit" do\n'
        f'    url "{SDIST_URL}"\n'
        f'    sha256 "{SDIST_SHA256}"\n'
        "  end"
    )


def uv_source_table() -> str:
    """``[tool.uv.sources]`` block derived from this pin for Corral's pyproject.

    Canonical direction: this module owns the pin (VERSION / WHEEL_URL);
    ``pyproject.toml`` mirrors the generated block verbatim. uv-only metadata:
    published install paths keep using the plain ``sesskit>=…`` range.
    """
    return f'[tool.uv.sources]\nsesskit = {{ url = "{WHEEL_URL}" }}\n'


def _sha256_url(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "corral-sesskit-dep"})
    last_err: Exception | None = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return hashlib.sha256(resp.read()).hexdigest()
        except Exception as exc:  # noqa: BLE001 - retry transient CDN/TLS blips
            last_err = exc
    raise RuntimeError(f"download failed: {url} ({last_err})")


def verify_published_digests() -> None:
    """Fail loudly if the Release assets no longer match the pinned digests."""
    wheel = _sha256_url(WHEEL_URL)
    sdist = _sha256_url(SDIST_URL)
    errors: list[str] = []
    if wheel != WHEEL_SHA256:
        errors.append(f"wheel sha256 mismatch: got {wheel}, want {WHEEL_SHA256}")
    if sdist != SDIST_SHA256:
        errors.append(f"sdist sha256 mismatch: got {sdist}, want {SDIST_SHA256}")
    if errors:
        raise SystemExit("; ".join(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "field",
        nargs="?",
        choices=(
            "version",
            "requires",
            "wheel-url",
            "wheel-sha256",
            "sdist-url",
            "sdist-sha256",
            "wheel-requirement",
            "uv-source-table",
            "json",
            "verify",
        ),
        default="wheel-requirement",
        help="Which pin field to print (default: wheel-requirement for pip)",
    )
    args = parser.parse_args(argv)
    if args.field == "version":
        print(VERSION)
    elif args.field == "requires":
        print(REQUIRES)
    elif args.field == "wheel-url":
        print(WHEEL_URL)
    elif args.field == "wheel-sha256":
        print(WHEEL_SHA256)
    elif args.field == "sdist-url":
        print(SDIST_URL)
    elif args.field == "sdist-sha256":
        print(SDIST_SHA256)
    elif args.field == "wheel-requirement":
        print(wheel_requirement())
    elif args.field == "uv-source-table":
        print(uv_source_table(), end="")
    elif args.field == "json":
        json.dump(
            {
                "version": VERSION,
                "requires": REQUIRES,
                "wheel_url": WHEEL_URL,
                "wheel_sha256": WHEEL_SHA256,
                "sdist_url": SDIST_URL,
                "sdist_sha256": SDIST_SHA256,
                "wheel_requirement": wheel_requirement(),
            },
            sys.stdout,
            indent=2,
        )
        print()
    elif args.field == "verify":
        verify_published_digests()
        print(f"ok sesskit {VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
