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
VERSION = "0.3.11"
TAG = f"v{VERSION}"
REPO = "x0c/sesskit"
PURE_WHEEL_NAME = f"sesskit-{VERSION}-py3-none-any.whl"
PURE_WHEEL_URL = f"https://github.com/{REPO}/releases/download/{TAG}/{PURE_WHEEL_NAME}"
MACOS_WHEEL_NAME = (
    f"sesskit-{VERSION}-cp310-abi3-"
    "macosx_10_12_x86_64.macosx_11_0_arm64.macosx_10_12_universal2.whl"
)
MACOS_WHEEL_URL = f"https://github.com/{REPO}/releases/download/{TAG}/{MACOS_WHEEL_NAME}"
SDIST_NAME = f"sesskit-{VERSION}.tar.gz"
SDIST_URL = f"https://github.com/{REPO}/releases/download/{TAG}/{SDIST_NAME}"
# sha256 of the built/published v0.3.11 assets (verified 2026-11-19).
PURE_WHEEL_SHA256 = "1f6b4bcabdbf7f6be82e7283546ab7bfd2c96e725a078629c74d1aa0b8f6944f"
MACOS_WHEEL_SHA256 = "ad0998854171b0d73d2f13a721efcf330d9c5976fab9ab1bb86ede02f96c00d3"
SDIST_SHA256 = "4f2332e7215af5f3a93160d1929231070f8a71369b5b5fdc97dc29a14553e5b7"
# Minimum version declared in Corral's pyproject.toml dependencies.
REQUIRES = f"sesskit>={VERSION}"


def _select_wheel(platform_name: str | None = None) -> tuple[str, str, str]:
    """Return ``(name, url, sha256)`` for ``platform_name`` (default: current OS).

    macOS installs the prebuilt universal2 native wheel so ``sesskit._native``
    accelerates Codex/OpenCode scanning and lowers Python allocator churn; other
    platforms keep the universal pure-Python compatibility wheel.
    """
    plat = sys.platform if platform_name is None else platform_name
    if plat == "darwin":
        return MACOS_WHEEL_NAME, MACOS_WHEEL_URL, MACOS_WHEEL_SHA256
    return PURE_WHEEL_NAME, PURE_WHEEL_URL, PURE_WHEEL_SHA256


WHEEL_NAME, WHEEL_URL, WHEEL_SHA256 = _select_wheel()


def wheel_requirement(platform_name: str | None = None) -> str:
    """PEP 508 direct reference with digest for pip."""
    if platform_name is None:
        return f"sesskit @ {WHEEL_URL}#sha256={WHEEL_SHA256}"
    _name, url, sha256 = _select_wheel(platform_name)
    return f"sesskit @ {url}#sha256={sha256}"


def homebrew_resource_block() -> str:
    """Ruby ``resource "sesskit"`` block for the Homebrew formula generator."""
    return (
        "  on_macos do\n"
        '    resource "sesskit" do\n'
        f'      url "{MACOS_WHEEL_URL}"\n'
        f'      sha256 "{MACOS_WHEEL_SHA256}"\n'
        "    end\n"
        "  end\n"
        "  on_linux do\n"
        '    resource "sesskit" do\n'
        f'      url "{PURE_WHEEL_URL}"\n'
        f'      sha256 "{PURE_WHEEL_SHA256}"\n'
        "    end\n"
        "  end"
    )


def uv_source_table() -> str:
    """``[tool.uv.sources]`` block derived from this pin for Corral's pyproject.

    Canonical direction: this module owns the pin (VERSION / WHEEL_URL);
    ``pyproject.toml`` mirrors the generated block verbatim. uv-only metadata:
    published install paths keep using the plain ``sesskit>=…`` range.
    """
    return (
        "[tool.uv.sources]\n"
        "sesskit = [\n"
        f'  {{ url = "{MACOS_WHEEL_URL}", marker = "sys_platform == \'darwin\'" }},\n'
        f'  {{ url = "{PURE_WHEEL_URL}", marker = "sys_platform != \'darwin\'" }},\n'
        "]\n"
    )


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
    checks = (
        ("macos wheel", MACOS_WHEEL_URL, MACOS_WHEEL_SHA256),
        ("pure wheel", PURE_WHEEL_URL, PURE_WHEEL_SHA256),
        ("sdist", SDIST_URL, SDIST_SHA256),
    )
    errors: list[str] = []
    for label, url, expected in checks:
        actual = _sha256_url(url)
        if actual != expected:
            errors.append(f"{label} sha256 mismatch: got {actual}, want {expected}")
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
