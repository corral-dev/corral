#!/usr/bin/env bash
# Build every release package this host can produce, plus the source package.
#
# macOS builds the universal2 wheel and, when Zig is available, the four Linux
# wheels (glibc/musl × x86_64/aarch64) by cross-linking. Linux builds its own
# wheels and cannot produce the macOS one. Packages that were not built are
# listed in OUT/not-built.txt (empty when the release set is complete), so the
# caller never uploads a partial set silently.
#
# Usage: bash scripts/build-dist.sh OUT_DIR
set -euo pipefail

OUT="${1:?usage: build-dist.sh OUT_DIR}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# universal2 needs rustup's toolchain (it carries the x86_64 std); Homebrew's
# rustc does not (see MAINTAINER_GUIDE "本机打 universal2 时 PATH").
if [ -x "$HOME/.cargo/bin/rustc" ]; then
  export PATH="$HOME/.cargo/bin:$PATH"
fi
command -v maturin >/dev/null || { echo "error: maturin not found; pip install 'maturin>=1.9,<2'" >&2; exit 1; }
mkdir -p "$OUT"

have_zig() { command -v zig >/dev/null 2>&1 || python3 -c "import ziglang" >/dev/null 2>&1; }

LINUX_TARGETS=(
  "x86_64-unknown-linux-gnu manylinux_2_17"
  "aarch64-unknown-linux-gnu manylinux_2_17"
  "x86_64-unknown-linux-musl musllinux_1_2"
  "aarch64-unknown-linux-musl musllinux_1_2"
)

build_linux_cross() {
  # abi3 cross builds need an explicit interpreter version for PyO3.
  local spec
  for spec in "${LINUX_TARGETS[@]}"; do
    set -- $spec
    rustup target add "$1" >/dev/null 2>&1 || true
    echo "==> Building $1 ($2)"
    PYO3_CROSS_PYTHON_VERSION=3.10 maturin build --release --out "$OUT" \
      --target "$1" --compatibility "$2" --zig -i python3
  done
}

NOT_BUILT=""
case "$(uname -s)" in
  Darwin)
    echo "==> Building universal2-apple-darwin"
    maturin build --release --out "$OUT" --target universal2-apple-darwin
    if have_zig; then
      build_linux_cross
    else
      NOT_BUILT="Linux wheels (install zig to cross-build them on macOS)"
    fi
    ;;
  Linux)
    if have_zig; then
      build_linux_cross
    else
      maturin build --release --out "$OUT" --compatibility manylinux_2_17
      NOT_BUILT="other Linux wheels (install ziglang to cross-build them)"
    fi
    NOT_BUILT="${NOT_BUILT:+$NOT_BUILT; }macOS universal2 wheel (build on a Mac)"
    ;;
  *) echo "error: unsupported build platform $(uname -s)" >&2; exit 1 ;;
esac
maturin sdist --out "$OUT"

printf '%s' "$NOT_BUILT" > "$OUT/not-built.txt"
echo "==> Built $(find "$OUT" -maxdepth 1 -name '*.whl' | wc -l | tr -d ' ') wheels and $(find "$OUT" -maxdepth 1 -name '*.tar.gz' | wc -l | tr -d ' ') sdist${NOT_BUILT:+; not built: $NOT_BUILT}"
