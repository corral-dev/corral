#!/usr/bin/env bash
# 把仓库内 .githooks 接到 Git 实际会执行的 hooks 目录。
#
# 本机若配置了全局 core.hooksPath（如 ~/.git-hooks），Git 会忽略各仓 .git/hooks。
# 此时绝不能把「只服务本仓」的脚本直接盖到全局 hooks 上——否则其它仓库推送也会跑
# corral 的检查。做法：在 hooksPath 放一个通用分发器，仅当当前仓库有
# `.githooks/pre-push` 时才转调。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

git rev-parse --git-dir >/dev/null || {
  echo "错误：当前不在 git 仓库内" >&2
  exit 1
}

chmod +x "$ROOT/.githooks/pre-push"

HOOKS_DIR="$(git rev-parse --git-path hooks)"
# 解析成绝对路径，便于判断是否落在本仓外
HOOKS_ABS="$(cd "$HOOKS_DIR" 2>/dev/null && pwd -P || python3 -c "import pathlib; print(pathlib.Path(r'''$HOOKS_DIR''').resolve())")"
ROOT_ABS="$(pwd -P)"

is_shared=0
case "$HOOKS_ABS" in
  "$ROOT_ABS"/*|"$ROOT_ABS"/.git/*) ;;
  *) is_shared=1 ;;
esac

# 通用分发器：任意带 .githooks/pre-push 的仓库都能用，不绑死 corral 路径。
# 本机常与 leakgate 共用 ~/.git-hooks/pre-push：必须先跑泄漏门禁，再转调仓内钩子。
# 禁止再写入「仅 Corral」的旧分发器，否则会盖掉全局密钥扫描。
write_dispatcher() {
  local dest="$1"
  # 若 dest 是指向本仓脚本的软链，cat > 会顺着链把真脚本盖掉——先拆链再写
  if [ -L "$dest" ]; then
    rm -f "$dest"
  fi
  cat >"$dest" <<'EOF'
#!/usr/bin/env bash
# 全局 pre-push：先跑 leakgate 泄漏门禁，再转调仓库内 .githooks/pre-push（如 Corral）。
# 由 Corral scripts/install-git-hooks.sh 与 agentsync leakgate 共同约定；勿改成只保留一侧。
# 关键一：stdin 只能读一次，先原样落到临时文件，两侧都从文件重放。
#         用 $(cat) + printf '%s' 会丢掉最后一行的结尾换行，while read 的消费方看不到它。
# 关键二：把 git 传入的 $@（远端名 / 远端地址）原样转交，可见性按真实推送目标判定。
set -uo pipefail

refs="$(mktemp "${TMPDIR:-/tmp}/leakgate-refs.XXXXXX")"
trap 'rm -f "$refs"' EXIT
cat > "$refs"

status=0

# --- leakgate：对本机所有仓库生效 ---
if [ "$(git config --bool --get leakgate.disabled 2>/dev/null || true)" != "true" ]; then
  LEAKGATE_PY="${HOME}/.config/agentsync/scripts/leakgate/leakgate.py"
  if [ -f "$LEAKGATE_PY" ]; then
    PY=""
    for candidate in python3 python; do
      if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys' >/dev/null 2>&1; then
        PY="$candidate"
        break
      fi
    done
    if [ -z "$PY" ]; then
      echo "leakgate: 找不到可用的 python3，本次 push 未经泄漏扫描" >&2
    else
      "$PY" "$LEAKGATE_PY" prepush "$@" < "$refs" || status=$?
    fi
  else
    echo "leakgate: 未找到扫描器 $LEAKGATE_PY，本次 push 未经泄漏扫描（请检查 agentsync 是否已同步）" >&2
  fi
fi

if [ "$status" -ne 0 ]; then
  exit "$status"
fi

# --- 仓库级钩子分发（Corral 等）---
root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
hook="$root/.githooks/pre-push"
if [ -x "$hook" ]; then
  "$hook" "$@" < "$refs" || exit $?
fi
exit 0
EOF
  chmod +x "$dest"
}

mkdir -p "$HOOKS_ABS"

if [ "$is_shared" -eq 1 ]; then
  dest="$HOOKS_ABS/pre-push"
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    # 已有实体文件：合并版 / 旧分发器可直接覆盖更新；其它内容先备份
    if ! grep -qE 'leakgate|由 corral scripts/install-git-hooks.sh 安装的通用 pre-push 分发器|全局 pre-push：先跑 leakgate' "$dest" 2>/dev/null; then
      bak="$dest.backup-$(date +%Y%m%d%H%M%S)"
      cp -p "$dest" "$bak"
      echo "已备份原有 $dest → $bak"
    fi
  fi
  # 若当前是指向本仓专用脚本的错误软链（旧版安装），改成「leakgate + 仓内钩子」合并分发器
  write_dispatcher "$dest"
  echo "已安装合并分发器（leakgate + .githooks/pre-push）: $dest"
  echo "检测到共享 hooksPath: $HOOKS_ABS"
  echo "本仓检查脚本: $ROOT/.githooks/pre-push"
else
  mkdir -p "$HOOKS_ABS"
  rel="$(python3 -c "
import os, pathlib
src = pathlib.Path(r'''$ROOT/.githooks/pre-push''').resolve()
dst_dir = pathlib.Path(r'''$HOOKS_ABS''').resolve()
print(os.path.relpath(src, dst_dir))
")"
  ln -sfn "$rel" "$HOOKS_ABS/pre-push"
  echo "已安装 $HOOKS_ABS/pre-push → $rel"
fi

echo "==> 日常推送只拦 ruff；release: 提交或 v* 标签推送跑全量。"
