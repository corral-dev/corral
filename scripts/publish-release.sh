#!/usr/bin/env bash
# 发版收尾：不等 GitHub Actions 排队，直接把新版本送到用户手上。
#
# 背景：`.github/workflows/release.yml` 负责构建各平台安装包、补 Release 附件、
# 更新 Homebrew 配方；但 GitHub 免费额度的并发上限（尤其 macOS 只有 5 个并发槽，
# 且按账号而非按仓库计）经常让整批任务排队几十分钟。真实发生过：v0.24.25 的
# release 任务排了 45 分钟仍未开始，用户 `brew upgrade` 拿到的还是三个版本前的
# 配方。本脚本把「用户能不能升级」这件事从 CI 的排队里摘出来，本机几十秒做完：
#
#   1. 确保 tag 对应的 GitHub Release 存在；
#   2. 构建**本机平台**能出的安装包 + 源码包，上传到该 Release；
#   3. 重新生成 Homebrew 配方：有预编译 wheel 的平台直装（不拉 Rust），
#      缺 wheel 的平台回退源码编译（本机出不了的平台包等 CI 补齐后重跑本
#      脚本即可切回 wheel），所以 macOS 用户即使没等到 CI 也能立刻升级）。
#
# CI 之后跑完会补齐本机出不了的那部分附件（在 Linux 上发版就是 macOS 预编译包，
# 反之亦然），并对同名附件做覆盖上传，两边不冲突。
#
# 用法（在 cli/ 目录，或用绝对路径）：
#   bash scripts/publish-release.sh            # 版本号取 pyproject.toml
#   bash scripts/publish-release.sh v0.24.26   # 显式指定
#
# 可选环境变量：
#   CORRAL_SKIP_WHEELS=1   跳过构建/上传安装包
#   CORRAL_SKIP_TAP=1      跳过更新 Homebrew 配方
# 中断恢复：若在「计算源码归档校验和」一步因境外网络超时中断（附件已传好、
# 只差配方），直接 `CORRAL_SKIP_WHEELS=1 bash scripts/publish-release.sh` 补跑即可
# （2026-08-17 v0.24.131 实踩）。
#   HOMEBREW_TAP_TOKEN     写 tap 仓库用的令牌（默认取 `gh auth token`）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

TAP_REPO="${CORRAL_TAP_REPO:-x0c/homebrew-tap}"
SOURCE_REPO="${CORRAL_REPO:-x0c/corral}"

die() { echo "错误：$*" >&2; exit 1; }

command -v gh >/dev/null || die "未找到 gh 命令，无法操作 GitHub Release"
gh auth status >/dev/null 2>&1 || die "gh 未登录，先执行 gh auth login"

TAG="${1:-}"
if [ -z "$TAG" ]; then
  TAG="v$(python3 -c '
import re, pathlib
text = pathlib.Path("pyproject.toml").read_text(encoding="utf-8")
print(re.search(r"^version = \"([^\"]+)\"", text, re.M).group(1))
')"
fi
VERSION="${TAG#v}"
echo "==> 发布 ${TAG}"

# 发版硬门槛：与 CI 同源的 ruff + 全量单测。推送门禁若被 --no-verify 绕过且戳
# 也失效，这里仍拦住「装坏包 / 把配方指到未经验证的 tag」。
if python3 scripts/ci-test.py --check-stamp >/dev/null; then
  echo "==> 完整检查已在本工作区跑过（产品代码未改），跳过重复"
else
  echo "==> 发版前强制跑 CI 同源检查"
  env -u TEXTUAL_DISABLE_KITTY_KEY PYTHONPATH=src python3 scripts/ci-test.py \
    || die "CI 同源检查未过，禁止发版收尾"
fi

# 干净环境安装：证明不依赖本机 editable SessKit / PYTHONPATH。
if [ "${CORRAL_SKIP_CLEAN_INSTALL:-0}" = "1" ]; then
  echo "==> 跳过干净环境安装检查（CORRAL_SKIP_CLEAN_INSTALL=1）"
else
  echo "==> 干净环境安装检查"
  python3 scripts/verify_clean_install.py --with-remote \
    || die "干净环境装不上，禁止发版收尾"
fi

git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null \
  || die "本地没有 ${TAG} 标签，先打好标签再跑本脚本"
git ls-remote --exit-code --tags github "refs/tags/${TAG}" >/dev/null 2>&1 \
  || die "${TAG} 还没推到 github 远端，先 git push github --tags"

# 按当前工作区构建安装包，不会切回 tag 源码。版本号对不上就会把更新版本的包传到旧 Release。
# 2026-08-16：给 v0.24.125 收尾时工作区已被并行 Agent 升到 0.24.126，macOS 包被传到 125。
WT_VER="$(python3 -c '
import re, pathlib
text = pathlib.Path("pyproject.toml").read_text(encoding="utf-8")
print(re.search(r"^version = \"([^\"]+)\"", text, re.M).group(1))
')"
if [ "$WT_VER" != "$VERSION" ]; then
  die "工作区版本是 ${WT_VER}，与 ${TAG} 不一致。把工作区回到该 tag 再跑，或改跑更新版本的收尾。"
fi

# ---- 1. Release 本体 ----
if gh release view "$TAG" >/dev/null 2>&1; then
  echo "==> Release ${TAG} 已存在"
else
  echo "==> 创建 Release ${TAG}"
  gh release create "$TAG" --title "$TAG" --generate-notes
fi

# ---- 2. 安装包 ----
if [ "${CORRAL_SKIP_WHEELS:-0}" = "1" ]; then
  echo "==> 跳过安装包构建（CORRAL_SKIP_WHEELS=1）"
else
  command -v maturin >/dev/null || die "未找到 maturin，先执行 pip install 'maturin>=1.9,<2'"
  DIST="$(mktemp -d)"
  trap 'rm -rf "$DIST"' EXIT
  case "$(uname -s)" in
    Darwin)
      # macOS 只出 universal2：Intel / Apple Silicon 共用一个包。
      maturin build --release --out "$DIST" --target universal2-apple-darwin
      SKIPPED="Linux 各架构预编译包（需在 Linux 上发版或等 CI 补齐）"
      ;;
    Linux)
      # 交叉编译靠 zig；缺了就只出本机架构，不静默少传。
      ZIG_ARG=()
      if python3 -c "import ziglang" >/dev/null 2>&1; then
        ZIG_ARG=(--zig)
      else
        echo "提示：未安装 ziglang（pip install ziglang），本轮只出本机架构" >&2
      fi
      maturin build --release --out "$DIST" --compatibility manylinux_2_17 "${ZIG_ARG[@]}"
      if [ ${#ZIG_ARG[@]} -gt 0 ]; then
        for spec in \
          "aarch64-unknown-linux-gnu manylinux_2_17" \
          "x86_64-unknown-linux-musl musllinux_1_2" \
          "aarch64-unknown-linux-musl musllinux_1_2"
        do
          set -- $spec
          rustup target add "$1" >/dev/null 2>&1 || true
          maturin build --release --out "$DIST" --target "$1" --compatibility "$2" --zig
        done
      fi
      SKIPPED="macOS 预编译包（需在 Mac 上发版或等 CI 补齐）"
      ;;
    *) die "不支持的发版平台：$(uname -s)" ;;
  esac
  maturin sdist --out "$DIST"

  # 附件里的 SHA256SUMS 只覆盖本机这批；CI 补齐后会整体覆盖成全量清单。
  python3 - "$DIST" <<'PY'
import hashlib, pathlib, sys
dist = pathlib.Path(sys.argv[1])
lines = []
for item in sorted(dist.iterdir()):
    if item.name == "SHA256SUMS":
        continue
    digest = hashlib.sha256(item.read_bytes()).hexdigest()
    lines.append(f"{digest}  {item.name}")
(dist / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

  echo "==> 上传安装包"
  # 打完 tag 之后 CI 的 release 工作流也在传同名附件，而 --clobber 是「先删再传」：
  # 两边交错时本机这次会拿到 404（附件 ID 在上传途中被对方删掉了）。这里绝不能让
  # 上传失败中断整个脚本——后面的 Homebrew 配方才是决定"用户能不能升级"的那一步，
  # 附件没传成最多是少个预编译包，配方停在旧版本才是真事故。
  # 2026-07-31 v0.24.29 实测踩到：脚本在这里退出，配方全靠 CI 恰好跑赢才没停在旧版。
  if ! gh release upload "$TAG" "$DIST"/* --clobber; then
    echo "!!  上传失败，等 8 秒重试一次（多半是和 CI 的 release 工作流抢同名附件）"
    sleep 8
    if ! gh release upload "$TAG" "$DIST"/* --clobber; then
      UPLOAD_FAILED=1
      echo "!!  安装包上传仍未成功；继续往下走，收尾核对会列出 Release 实际有几个附件"
    fi
  fi
  echo "==> 本轮未覆盖：${SKIPPED}"
fi

# ---- 3. Homebrew 配方 ----
if [ "${CORRAL_SKIP_TAP:-0}" = "1" ]; then
  echo "==> 跳过 Homebrew 配方（CORRAL_SKIP_TAP=1）"
else
  TOKEN="${HOMEBREW_TAP_TOKEN:-$(gh auth token)}"
  [ -n "$TOKEN" ] || die "拿不到可写 ${TAP_REPO} 的令牌"
  WORK="$(mktemp -d)"
  git clone -q "https://x-access-token:${TOKEN}@github.com/${TAP_REPO}.git" "$WORK/tap"
  # 配方由 scripts/homebrew_formula.py 整体生成：有预编译 wheel 的平台直装（不拉
  # Rust），缺 wheel 的平台回退源码编译。本机出不了的平台包（如 Mac 发版时的
  # Linux wheel）对应平台自动落到源码兜底，等 CI 补齐附件后重跑本脚本即可切回
  # wheel。退出码 3 = 配方已是更新的版本，属正常跳过，不能让 set -e 把脚本带走。
  rc=0
  GITHUB_API_TOKEN="$TOKEN" python3 scripts/homebrew_formula.py \
    --tag "$TAG" --tap-dir "$WORK/tap" --repo "${SOURCE_REPO}" || rc=$?
  if [ "$rc" -eq 0 ]; then
    if git -C "$WORK/tap" diff --quiet -- Formula/corral.rb; then
      echo "==> 配方已经指向 ${TAG}，无需改动"
    else
      git -C "$WORK/tap" -c user.name="x0c" -c user.email="x0c@users.noreply.github.com" \
        commit -q -am "corral ${VERSION}"
      git -C "$WORK/tap" push -q origin main
      echo "==> 配方已更新到 ${VERSION}"
    fi
  elif [ "$rc" -ne 3 ]; then
    rm -rf "$WORK"; die "生成配方失败"
  fi
  rm -rf "$WORK"
fi

# The installed `corral` (usually a pipx venv) keeps its own SessKit copy; a
# release that raises the pin does nothing for this machine until that copy
# matches (2026-09-30: two releases shipped while the local install and the
# phone daemon still ran an older SessKit without the incremental readers).
sync_local_sesskit() {
  command -v corral >/dev/null 2>&1 || return 0
  local py want have
  py=$(corral --version 2>/dev/null | sed -n 's/^ *python: *//p' | head -1)
  [ -n "$py" ] && [ -x "$py" ] || return 0
  want=$(python3 -c 'import sys; sys.path.insert(0, "scripts"); import sesskit_dep; print(sesskit_dep.VERSION)')
  have=$("$py" -c 'import sesskit; print(sesskit.__version__)' 2>/dev/null || echo none)
  if [ "$have" = "$want" ]; then
    echo "==> 本机 corral 的 SessKit 已是 ${want}"
    return 0
  fi
  echo "==> 本机 corral 的 SessKit ${have} → ${want}"
  "$py" -m pip install -q --upgrade "$(python3 -c 'import sys; sys.path.insert(0, "scripts"); import sesskit_dep; print(sesskit_dep.wheel_requirement())')" \
    || { echo "!!  本机 SessKit 升级失败，请按 docs/MAINTAINER_GUIDE.md 手动安装"; return 0; }
  if corral remote status 2>/dev/null | grep -q '^Status: on'; then
    corral remote off >/dev/null 2>&1 && corral remote on >/dev/null 2>&1 \
      && echo "==> 手机远程服务已重启，加载新 SessKit"
  fi
}
sync_local_sesskit

echo
echo "==> 收尾核对"
gh release view "$TAG" --json tagName,assets \
  --jq '"Release \(.tagName)：\(.assets | length) 个附件"'
curl -fsSL "https://raw.githubusercontent.com/${TAP_REPO}/main/Formula/corral.rb" \
  | grep -E '^\s*url ' | sed 's/^/配方 /'
curl -fsSL "https://api.github.com/repos/${SOURCE_REPO}/releases/latest" \
  | python3 -c 'import json,sys; print("最新 Release：" + json.load(sys.stdin)["tag_name"])'
if [ "${UPLOAD_FAILED:-0}" = "1" ]; then
  echo "!!  注意：本机这轮安装包上传失败过，请对照上面的附件数量确认是否需要重跑本脚本"
  exit 1
fi
