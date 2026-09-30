#!/usr/bin/env bash
# 把本仓库以 editable 方式装到「corral 命令实际用的解释器」上。
# 解决：改了 cli/src，敲 corral 却仍跑 pipx/site-packages 旧副本。
#
# 用法（在任意目录）：
#   bash /path/to/corral/cli/scripts/dev-install.sh
# 或：
#   cd cli && bash scripts/dev-install.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f "$ROOT/pyproject.toml" || ! -f "$ROOT/src/corral/__init__.py" ]]; then
  echo "错误：未找到 corral 源码树（需要 pyproject.toml 与 src/corral/）: $ROOT" >&2
  exit 1
fi

# 改名前入口叫 pickup。只认 corral 时，旧安装会让脚本误走 pipx，装出一份
# 非 editable 副本，随后校验失败（2026-08-23 真机）。
entry_python() {
  local cmd bin shebang py
  for cmd in corral pickup; do
    bin="$(command -v "$cmd" 2>/dev/null || true)"
    if [[ -n "$bin" && -f "$bin" ]]; then
      # pipx 的 POSIX 入口首行只是 #!/bin/sh，真正解释器在第二行的 exec 中。
      py="$(sed -n "2s/^'''exec' '\([^']*\)'.*/\1/p" "$bin" 2>/dev/null || true)"
      if [[ -n "$py" && -x "$py" ]]; then
        printf '%s\n' "$py"
        return 0
      fi
      shebang="$(sed -n '1s/^#!//p' "$bin" 2>/dev/null || true)"
      if [[ -n "$shebang" && -x "$shebang" ]]; then
        printf '%s\n' "$shebang"
        return 0
      fi
    fi
  done
  return 1
}

install_editable() {
  local py="$1"
  echo "→ $py -m pip install --force-reinstall --no-deps -e $ROOT"
  if "$py" -m pip install --force-reinstall --no-deps -e "$ROOT"; then
    return 0
  fi

  # Homebrew Python 遵循 PEP 668，拒绝直接写其受管理目录；用户目录仍会被该解释器优先加载，
  # 因此改为安全地把开发副本装到当前用户，而不是让开发安装半途失败。
  echo "检测到受管理的 Python，改装到当前用户目录…"
  "$py" -m pip install --user --break-system-packages --force-reinstall --no-deps -e "$ROOT"
}

install_pipx_editable() {
  echo "→ pipx install --editable --force $ROOT[remote]"
  pipx install --editable --force "$ROOT[remote]"
}

is_pipx_python() {
  local py="$1"
  "$py" -c 'import os, sys; raise SystemExit(0 if os.path.isfile(os.path.join(sys.prefix, "pipx_metadata.json")) else 1)'
}

PY=""
if PY="$(entry_python)"; then
  echo "检测到入口解释器：$PY"
  if is_pipx_python "$PY"; then
    install_pipx_editable
    PY="$(entry_python)"
  else
    install_editable "$PY"
  fi
elif command -v pipx >/dev/null 2>&1; then
  echo "未找到可用的 corral/pickup shebang，改用 pipx install …"
  install_pipx_editable
  PY="$(entry_python || true)"
  if [[ -z "${PY:-}" ]]; then
    echo "错误：pipx 安装后仍找不到 corral 入口" >&2
    exit 1
  fi
else
  echo "未找到 corral / pickup / pipx，改用 python3 --user editable 安装"
  PY="$(command -v python3)"
  install_editable "$PY"
fi

echo ""
echo "校验加载路径："
CHECK_PY="${PY:-$(command -v python3)}"
if command -v corral >/dev/null 2>&1; then
  ENTRY_PY="$(entry_python || true)"
  if [[ -n "${ENTRY_PY:-}" ]]; then
    CHECK_PY="$ENTRY_PY"
  fi
fi
"$CHECK_PY" -c "
import corral, os, sys
path = os.path.abspath(corral.__file__)
print('  version:', corral.__version__)
print('  file:   ', path)
print('  python: ', sys.executable)
ok = os.path.samefile(os.path.dirname(path), r'$ROOT/src/corral') or path.startswith(r'$ROOT/src/corral' + os.sep)
print('  editable指向本仓库:', '是' if ok else '否（请检查上方 pip 输出）')
raise SystemExit(0 if ok else 1)
"

if command -v pickup >/dev/null 2>&1 && ! command -v corral >/dev/null 2>&1; then
  echo "警告：PATH 上仍只有旧命令 pickup=$(command -v pickup)，没有 corral。" >&2
elif command -v pickup >/dev/null 2>&1; then
  echo "提示：旧命令 pickup 仍在 PATH（$(command -v pickup)）。确认 corral 可用后可卸载：python3 -m pip uninstall pickup  或  pipx uninstall pickup"
fi

echo ""
echo "完成。命令拦截默认关闭；需要「敲原命令进托管」时再执行：corral shim install"
echo "日常开发：改 src/ 后直接再跑 corral 即可，无需反复 force-reinstall。"
