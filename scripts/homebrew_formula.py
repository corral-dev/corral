#!/usr/bin/env python3
"""生成 Corral 的 Homebrew 配方：预编译 wheel 优先，缺 wheel 的平台回退源码编译。

背景（2026-08-23）：旧配方永远从源码归档安装，`brew install/upgrade corral`
每次都要拉 Rust 工具链现场编译，用户实测极慢。而发版流程本来就往 GitHub
Release 上传各平台预编译 wheel（macOS universal2 一个包通吃双架构所有系统
版本）。本生成器按 Release 附件实际情况逐平台决定安装方式：

  - macOS：有 universal2 wheel 就直装（不依赖 Rust）；
  - Linux x86_64 / aarch64：有 manylinux wheel 就直装；
  - 某平台缺 wheel 时，该平台的 url 指向源码归档、并声明 maturin/rust
    构建依赖兜底（构建依赖按平台包在 on_macos/on_linux 里，不给有
    wheel 的平台平白拉 Rust）。

机制依据（读 Homebrew 源码 + 实测确认，改动前先复核是否仍成立）：
  - brew 下载 `.whl` 走 Uncompressed 解包策略（AbstractDownloadStrategy#stage
    以 prioritize_extension 调 UnpackStrategy.detect，.whl 不在任何扩展名
    策略里），文件原样落在 buildpath，`Dir["*.whl"]` 直接可装；
  - `venv.pip_install` 的 std_pip_args 带 `--no-binary=:all:`，但不阻止
    直接安装 wheel 文件路径（2026-08-23 pip 实测）。

用法（发版流程内部使用，也可单独跑）：
  python3 scripts/homebrew_formula.py --tag v0.24.144 --tap-dir /path/to/tap
  python3 scripts/homebrew_formula.py --tag v0.24.144 --output corral.rb  # 只生成不写 tap

退出码：0 = 已写入或内容无变化；3 = tap 里配方已是更新版本，正常跳过；1 = 失败。

离线测试钩子（单元测试/网络不可达时用，互不冲突）：
  --assets-json FILE   附件名列表 JSON（替代 GitHub API 查询）
  --sha256sums FILE   本地 SHA256SUMS 文件路径（替代下载该附件）
  --source-sha256 HEX 源码归档 sha256（跳过下载归档计算）
"""

import argparse
import hashlib
import importlib
import json
import os
import pathlib
import re
import sys
import urllib.request

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
# Path-dependent sibling import: importlib keeps the required sys.path setup
# without tripping E402 (module import after runtime code). Name binding and
# behavior are identical to a plain `import sesskit_dep`.
sesskit_dep = importlib.import_module("sesskit_dep")

DESC = "Terminal session handoff tool for Claude Code, Codex CLI, OpenCode, Cursor, and Pi"

# 纯 Python 运行时依赖（textual 及其传递依赖）。Homebrew 安装阶段禁止联网，
# 每个依赖都要一个 resource 块（下载地址 + sha256）。依赖升级时同步改这里
# （可借助 `brew update-python-resources` / homebrew-pypi-poet 生成）。
# sesskit：真源在 scripts/sesskit_dep.py（与 CI / install.sh 同一份 pin）。
RESOURCES = f"""{sesskit_dep.homebrew_resource_block()}

  resource "linkify-it-py" do
    url "https://files.pythonhosted.org/packages/2e/c9/06ea13676ef354f0af6169587ae292d3e2406e212876a413bf9eece4eb23/linkify_it_py-2.1.0.tar.gz"
    sha256 "43360231720999c10e9328dc3691160e27a718e280673d444c38d7d3aaa3b98b"
  end

  resource "markdown-it-py" do
    url "https://files.pythonhosted.org/packages/06/ff/7841249c247aa650a76b9ee4bbaeae59370dc8bfd2f6c01f3630c35eb134/markdown_it_py-4.2.0.tar.gz"
    sha256 "04a21681d6fbb623de53f6f364d352309d4094dd4194040a10fd51833e418d49"
  end

  resource "mdit-py-plugins" do
    url "https://files.pythonhosted.org/packages/59/fc/f8d0863f8862f25602c0404d75568e89fb6b4109804645e5cdfb1be5cf56/mdit_py_plugins-0.6.1.tar.gz"
    sha256 "a2bca0f039f39dbd35fb74ae1b5f998608c437463371f0ff7f49a19a17a114d0"
  end

  resource "mdurl" do
    url "https://files.pythonhosted.org/packages/d6/54/cfe61301667036ec958cb99bd3efefba235e65cdeb9c84d24a8293ba1d90/mdurl-0.1.2.tar.gz"
    sha256 "bb413d29f5eea38f31dd4754dd7377d4465116fb207585f97bf925588687c1ba"
  end

  resource "platformdirs" do
    url "https://files.pythonhosted.org/packages/52/cd/4f25b2f95b23f5d2c9c1fe43e49841bff5800562149b2666afc09309aa8f/platformdirs-4.10.1.tar.gz"
    sha256 "ceab4084426fe6319ce18e86deada8ab1b7487c7aee7040c55e277c9ae793695"
  end

  resource "Pygments" do
    url "https://files.pythonhosted.org/packages/c3/b2/bc9c9196916376152d655522fdcebac55e66de6603a76a02bca1b6414f6c/pygments-2.20.0.tar.gz"
    sha256 "6757cd03768053ff99f3039c1a36d6c0aa0b263438fcab17520b30a303a82b5f"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/c0/8f/0722ca900cc807c13a6a0c696dacf35430f72e0ec571c4275d2371fca3e9/rich-15.0.0.tar.gz"
    sha256 "edd07a4824c6b40189fb7ac9bc4c52536e9780fbbfbddf6f1e2502c31b068c36"
  end

  resource "textual" do
    url "https://files.pythonhosted.org/packages/00/21/39a76b01bd5eea82a04baaca7580e105d8c59450df03998345bb2cfb307b/textual-8.2.8.tar.gz"
    sha256 "3f106a9fbc73e39dd266c9712432087de78a6d644084c7c241d6a25c3169115b"
  end

  resource "typing-extensions" do
    url "https://files.pythonhosted.org/packages/f6/cc/6253133b5bb138fc3306cebfbda2c520f545d36b5be2c7255cc528bb45d6/typing_extensions-4.16.0.tar.gz"
    sha256 "dc983d19a509c94dba722ee6abd33940f7c05a89e243c47e907eb4db6f1a43e5"
  end

  resource "uc-micro-py" do
    url "https://files.pythonhosted.org/packages/78/67/9a363818028526e2d4579334460df777115bdec1bb77c08f9db88f6389f2/uc_micro_py-2.0.0.tar.gz"
    sha256 "c53691e495c8db60e16ffc4861a35469b0ba0821fe409a8a7a0a71864d33a811"
  end"""


def http_get(url: str, token: str | None = None, timeout: int = 60) -> bytes:
    """带一次重试的 GET（网络冷启动超时不算失败）。"""
    headers = {"User-Agent": "corral-formula-generator"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last_err: Exception | None = None
    for _ in range(2):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - 统一重试后上抛
            last_err = exc
    raise RuntimeError(f"下载失败：{url}（{last_err}）")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ReleaseAssets:
    """Release 附件查询与 sha256 计算（优先复用 SHA256SUMS，避免重复下载大文件）。"""

    def __init__(
        self,
        repo: str,
        tag: str,
        token: str | None,
        assets_json: str | None,
        sums_file: str | None,
        source_sha: str | None,
    ):
        self.repo = repo
        self.tag = tag
        self.token = token
        self.source_sha_override = source_sha
        if assets_json:
            names = json.loads(pathlib.Path(assets_json).read_text(encoding="utf-8"))
            self.names = [n if isinstance(n, str) else n["name"] for n in names]
        else:
            self.names = self._fetch_names()
        self.sums: dict[str, str] = {}
        if sums_file:
            self.sums = parse_sums(pathlib.Path(sums_file).read_text(encoding="utf-8"))
        elif "SHA256SUMS" in self.names:
            self.sums = parse_sums(http_get(self.asset_url("SHA256SUMS"), token).decode("utf-8"))

    def _fetch_names(self) -> list[str]:
        url = f"https://api.github.com/repos/{self.repo}/releases/tags/{self.tag}"
        data = json.loads(http_get(url, self.token).decode("utf-8"))
        return [a["name"] for a in data.get("assets", [])]

    def asset_url(self, name: str) -> str:
        return f"https://github.com/{self.repo}/releases/download/{self.tag}/{name}"

    def find(self, pattern: str) -> str | None:
        for name in self.names:
            if re.search(pattern, name):
                return name
        return None

    def asset_sha(self, name: str) -> str:
        if name in self.sums:
            return self.sums[name]
        # SHA256SUMS 没覆盖（本机发版只传了部分附件）：下载该附件现算
        return sha256_bytes(http_get(self.asset_url(name), self.token))

    def source_archive(self) -> tuple[str, str]:
        """源码归档（GitHub 自动生成的 tag 归档，不是 Release 附件里的 sdist）。"""
        url = f"https://github.com/{self.repo}/archive/refs/tags/{self.tag}.tar.gz"
        if self.source_sha_override:
            return url, self.source_sha_override
        return url, sha256_bytes(http_get(url, self.token))


def parse_sums(text: str) -> dict[str, str]:
    sums = {}
    for line in text.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            sums[parts[1].strip().lstrip("*")] = parts[0]
    return sums


def version_parts(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def existing_formula_version(formula_path: pathlib.Path) -> str | None:
    """读 tap 里现有配方的版本（url 行里的 vX.Y.Z），用于防回退比较。"""
    if not formula_path.exists():
        return None
    text = formula_path.read_text(encoding="utf-8")
    versions = re.findall(r'^\s*url "https://github\.com/[^"]*?v(\d+(?:\.\d+)+)', text, re.M)
    return max(versions, key=version_parts) if versions else None


def build_formula(version: str, slots: dict) -> str:
    """组装配方。slots 键：macos / linux_x86_64 / linux_aarch64，值为 (url, sha, is_wheel)。"""

    def slot_url(indent: str, url: str, sha: str) -> str:
        return f'{indent}url "{url}"\n{indent}sha256 "{sha}"'

    mac = slots["macos"]
    lx64 = slots["linux_x86_64"]
    larm = slots["linux_aarch64"]

    blocks = ["  on_macos do\n" + slot_url("    ", mac[0], mac[1]) + "\n  end"]
    linux_inner = ""
    if lx64 != larm:
        linux_inner = (
            "    on_intel do\n" + slot_url("      ", lx64[0], lx64[1]) + "\n    end\n"
            "    on_arm do\n" + slot_url("      ", larm[0], larm[1]) + "\n    end\n"
        )
    else:
        linux_inner = slot_url("    ", lx64[0], lx64[1]) + "\n"
    blocks.append("  on_linux do\n" + linux_inner.rstrip("\n") + "\n  end")
    url_blocks = "\n\n".join(blocks)

    # 构建依赖只挂在需要源码编译的平台上，有 wheel 的平台不拉 Rust
    macos_source = not mac[2]
    linux_source = not (lx64[2] and larm[2])
    dep_lines = ""
    if macos_source or linux_source:
        deps = '    depends_on "maturin" => :build\n    depends_on "rust" => :build'
        if macos_source and linux_source:
            dep_lines = deps.replace("    ", "  ", 1).replace("\n    ", "\n  ")
        elif macos_source:
            dep_lines = "  on_macos do\n" + deps + "\n  end"
        else:
            dep_lines = "  on_linux do\n" + deps + "\n  end"

    parts = [
        "# 由 cli/scripts/homebrew_formula.py 生成，请勿手改；改生成器后随下次发版重新生成。",
        "# 安装策略：有预编译 wheel 的平台直接安装（不拉 Rust 工具链）；缺 wheel 的平台源码编译兜底。",
        "class Corral < Formula",
        "  include Language::Python::Virtualenv",
        "",
        f'  desc "{DESC}"',
        '  homepage "https://github.com/corral-dev/corral"',
        f'  version "{version}"',
        '  license "MIT"',
        "",
        url_blocks,
        "",
        '  depends_on "python@3.12"',
        '  depends_on "tmux"',
    ]
    if dep_lines:
        parts += ["", dep_lines]
    parts += [
        "",
        RESOURCES,
        "",
        "  def install",
        '    venv = virtualenv_create(libexec, "python3.12")',
        "    venv.pip_install resources",
        '    if (wheel = Dir["*.whl"].first)',
        "      # 预编译 wheel：直接安装，无需 Rust 工具链",
        "      venv.pip_install wheel",
        "    else",
        "      # 该平台无预编译包：源码编译兜底",
        '      system "maturin", "build", "--release", "--interpreter", libexec/"bin/python", "--out", "dist"',
        '      venv.pip_install Dir["dist/*.whl"].first',
        "    end",
        '    bin.install_symlink libexec/"bin/corral"',
        "  end",
        "",
        "  test do",
        '    assert_match "corral", shell_output("#{bin}/corral --help")',
        "  end",
        "end",
        "",
    ]
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Corral 的 Homebrew 配方（wheel 优先）")
    parser.add_argument("--tag", required=True, help="版本标签，如 v0.24.144")
    parser.add_argument("--tap-dir", help="tap 仓库目录（写入 Formula/corral.rb，含防回退）")
    parser.add_argument("--output", help="只把配方写到该文件（不写 tap）")
    parser.add_argument("--repo", default="corral-dev/corral", help="GitHub 源码仓（owner/name）")
    parser.add_argument("--assets-json", help=argparse.SUPPRESS)
    parser.add_argument("--sha256sums", help=argparse.SUPPRESS)
    parser.add_argument("--source-sha256", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if not args.tap_dir and not args.output:
        parser.error("必须指定 --tap-dir 或 --output 之一")

    version = args.tag.removeprefix("v")
    token = os.environ.get("GITHUB_API_TOKEN") or os.environ.get("GH_TOKEN")

    # 防回退：tap 里配方已是更新版本就跳过（排队积压的旧 tag 任务晚执行时不能把版本写回去）
    if args.tap_dir:
        formula_path = pathlib.Path(args.tap_dir) / "Formula" / "corral.rb"
        current = existing_formula_version(formula_path)
        if current and version_parts(current) > version_parts(version):
            print(f"配方已是更新的 {current}，跳过")
            return 3

    assets = ReleaseAssets(args.repo, args.tag, token, args.assets_json, args.sha256sums, args.source_sha256)

    source_url, source_sha = assets.source_archive()

    def slot(pattern: str) -> tuple[str, str, bool]:
        name = assets.find(pattern)
        if name:
            return assets.asset_url(name), assets.asset_sha(name), True
        return source_url, source_sha, False

    # macOS：优先 universal2；本机发版若只出了 arm64 wheel，也直装（Intel 回退源码）。
    macos = slot(r"macosx.*universal2\.whl$")
    if not macos[2]:
        macos = slot(r"macosx.*arm64\.whl$")

    slots = {
        "macos": macos,
        "linux_x86_64": slot(r"manylinux.*x86_64\.whl$"),
        "linux_aarch64": slot(r"manylinux.*aarch64\.whl$"),
    }

    plan = []
    for key, (url, _sha, is_wheel) in slots.items():
        kind = "预编译 wheel" if is_wheel else "源码编译（无 wheel，兜底）"
        plan.append(f"  {key}: {kind} <- {url.rsplit('/', 1)[-1]}")
    print("安装策略：\n" + "\n".join(plan))

    formula = build_formula(version, slots)

    if args.output:
        pathlib.Path(args.output).write_text(formula, encoding="utf-8")
        print(f"==> 配方已写入 {args.output}")
        return 0

    formula_path.parent.mkdir(parents=True, exist_ok=True)
    if formula_path.exists() and formula_path.read_text(encoding="utf-8") == formula:
        print(f"==> 配方已指向 {args.tag}，无需改动")
        return 0
    formula_path.write_text(formula, encoding="utf-8")
    print(f"==> 配方已更新到 {version}（{args.tap_dir}/Formula/corral.rb）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
