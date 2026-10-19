"""macOS TCC 身份：给 corral 一个稳定的授权身份，而不是 Python/tmux。

背景（细则见 ``docs/TCC_KNOWLEDGE_BASE.md``）：pip 装出来的 corral 是 Python
脚本，TCC 看到的是解释器（弹窗写 "Python"，一升级解释器授权就 orphan）；
托管在 tmux daemon 里的 agent 调截图/系统小工具时，责任链顶是 tmux server，
弹窗写 "tmux"。本模块提供一个 opt-in 的小跳板：

- ``corral tcc wrap``：用系统 ``cc`` 编一个 ~10KB 的 C 跳板，常驻父进程、
  spawn 真正的 Python（绝不 ``exec`` 换像，换像会丢掉责任链），ad-hoc 签名
  配稳定 identifier。跳板二进制字节一旦定稿就冻结（按源码 tag 缓存），
  Python 升级只改旁边的 sidecar JSON，不动二进制 → cdhash 不变 → 授权不掉。
- ``corral tcc status``：报告跳板 / 解释器 / tmux / 责任链的身份归因。
- ``corral tcc unwrap``：拆掉跳板与 sidecar（缓存保留，重装可原样恢复）。

本模块只依赖标准库 + legacy_names：bootstrap 走快速分发，不能在这里
import Textual/扫描器/网络。diagnose 经 ``identity_report()`` 复用探针。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from corral.legacy_names import cache_dir

IDENTIFIER = "com.x0c.corral.cli"
LAUNCHER_BASENAME = "corral-tcc"
SIDECAR_BASENAME = "corral-tcc.json"
MODULE_TARGET = "corral.bootstrap"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

# ad-hoc 签名默认不需要任何证书；机主若想用 Developer ID 做跨 rebuild 稳定，
# 显式传 --sign-identity 或设 CORRAL_TCC_SIGN_IDENTITY（见 wrap 文档）。
ADHOC_IDENTITY = "-"


class TccError(Exception):
    """用户可读的失败（缺工具链、非 macOS、目标自指等），不丢裸栈。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


# ---------------------------------------------------------------------------
# C 跳板源码：二进制必须与安装路径无关（路径只活在 sidecar 里），否则换个
# 安装位置就换 cdhash，稳定身份无从谈起。父进程常驻 spawn + 信号转发 + 退出
# 码透传；sidecar 解析只认机器写出的 {"python","argv_prefix"} 两段式。
# 改动本源码即换身份：source_tag() 会变，旧缓存自动失效，用户需重新授权一次。
# ---------------------------------------------------------------------------

TRAMPOLINE_C = r'''/* corral-tcc: stable macOS TCC identity launcher for corral.
 *
 * A pip-installed corral is a Python script, so TCC attributes its privacy
 * requests to whatever interpreter installed it ("Python..."), and every
 * interpreter upgrade orphans prior grants. This trampoline owns a stable
 * identity instead: it stays the parent process and spawns the real target,
 * so the responsibility chain keeps pointing at this binary.
 *
 * The binary must stay byte-identical across Python upgrades (its ad-hoc
 * cdhash IS the identity). Per-install paths live in the sidecar
 * "<launcher>.json" next to it, never baked into the binary:
 *   {"python": "/path/to/python", "argv_prefix": ["-m", "corral.bootstrap"]}
 * "argv_prefix" is prepended to the user's argv; "python" is exec'd.
 * When "python" is "" the prefix runs directly (shell-wrapper targets).
 */
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

static pid_t g_child = -1;

static void forward_signal(int sig) {
    pid_t child = g_child;
    if (child > 0) {
        kill(child, sig);
    }
}

static void install_forwarder(int sig) {
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = forward_signal;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = SA_RESTART;
    sigaction(sig, &sa, NULL);
}

/* ---- minimal sidecar parser (machine-written JSON only) ---- */

typedef struct {
    const char *p;
    const char *end;
} cursor_t;

static void skip_ws(cursor_t *c) {
    while (c->p < c->end && (*c->p == ' ' || *c->p == '\t' || *c->p == '\n' || *c->p == '\r')) {
        c->p++;
    }
}

/* Parse "..." with \" \\ \/ \n \t \r escapes; raw UTF-8 passes through. */
static char *parse_quoted(cursor_t *c) {
    if (c->p >= c->end || *c->p != '"') {
        return NULL;
    }
    c->p++;
    size_t cap = 256, len = 0;
    char *out = (char *)malloc(cap);
    if (!out) {
        return NULL;
    }
    while (c->p < c->end) {
        char ch = *c->p;
        if (ch == '"') {
            c->p++;
            out[len] = '\0';
            return out;
        }
        if (ch == '\\' && c->p + 1 < c->end) {
            char esc = c->p[1];
            char mapped = 0;
            if (esc == '"' || esc == '\\' || esc == '/') {
                mapped = esc;
            } else if (esc == 'n') {
                mapped = '\n';
            } else if (esc == 't') {
                mapped = '\t';
            } else if (esc == 'r') {
                mapped = '\r';
            }
            if (mapped) {
                ch = mapped;
                c->p += 2;
            } else {
                /* Unknown escape (incl. \u): copy both bytes verbatim. */
                if (len + 2 >= cap) {
                    cap *= 2;
                    char *grown = (char *)realloc(out, cap);
                    if (!grown) {
                        free(out);
                        return NULL;
                    }
                    out = grown;
                }
                out[len++] = '\\';
                out[len++] = esc;
                c->p += 2;
                continue;
            }
        } else {
            c->p++;
        }
        if (len + 1 >= cap) {
            cap *= 2;
            char *grown = (char *)realloc(out, cap);
            if (!grown) {
                free(out);
                return NULL;
            }
            out = grown;
        }
        out[len++] = ch;
    }
    free(out);
    return NULL;
}

static int expect_char(cursor_t *c, char want) {
    skip_ws(c);
    if (c->p < c->end && *c->p == want) {
        c->p++;
        return 1;
    }
    return 0;
}

/* Skip one JSON value we do not care about (string/array/object/number/lit). */
static int skip_value(cursor_t *c) {
    skip_ws(c);
    if (c->p >= c->end) {
        return 0;
    }
    if (*c->p == '"') {
        char *s = parse_quoted(c);
        if (!s) {
            return 0;
        }
        free(s);
        return 1;
    }
    if (*c->p == '[' || *c->p == '{') {
        char open = *c->p;
        char close = (open == '[') ? ']' : '}';
        int depth = 0;
        int in_str = 0;
        while (c->p < c->end) {
            char ch = *c->p++;
            if (in_str) {
                if (ch == '\\' && c->p < c->end) {
                    c->p++;
                } else if (ch == '"') {
                    in_str = 0;
                }
            } else if (ch == '"') {
                in_str = 1;
            } else if (ch == open) {
                depth++;
            } else if (ch == close) {
                depth--;
                if (depth == 0) {
                    return 1;
                }
            }
        }
        return 0;
    }
    while (c->p < c->end && *c->p != ',' && *c->p != '}' && *c->p != ']') {
        c->p++;
    }
    return 1;
}

typedef struct {
    char *python;
    char **prefix;
    int nprefix;
} sidecar_t;

static void sidecar_free(sidecar_t *s) {
    free(s->python);
    for (int i = 0; i < s->nprefix; i++) {
        free(s->prefix[i]);
    }
    free(s->prefix);
    s->python = NULL;
    s->prefix = NULL;
    s->nprefix = 0;
}

static int parse_sidecar(const char *text, sidecar_t *out) {
    cursor_t c = {text, text + strlen(text)};
    memset(out, 0, sizeof(*out));
    if (!expect_char(&c, '{')) {
        return 0;
    }
    skip_ws(&c);
    if (c.p < c.end && *c.p == '}') {
        return 0;
    }
    while (1) {
        skip_ws(&c);
        char *key = parse_quoted(&c);
        if (!key || !expect_char(&c, ':')) {
            free(key);
            return 0;
        }
        if (strcmp(key, "python") == 0) {
            skip_ws(&c);
            out->python = parse_quoted(&c);
            if (!out->python) {
                free(key);
                return 0;
            }
        } else if (strcmp(key, "argv_prefix") == 0) {
            if (!expect_char(&c, '[')) {
                free(key);
                return 0;
            }
            size_t cap = 8;
            out->prefix = (char **)malloc(cap * sizeof(char *));
            if (!out->prefix) {
                free(key);
                return 0;
            }
            skip_ws(&c);
            if (c.p < c.end && *c.p == ']') {
                c.p++;
            } else {
                while (1) {
                    skip_ws(&c);
                    char *item = parse_quoted(&c);
                    if (!item) {
                        free(key);
                        return 0;
                    }
                    if (out->nprefix + 1 >= (int)cap) {
                        cap *= 2;
                        char **grown = (char **)realloc(out->prefix, cap * sizeof(char *));
                        if (!grown) {
                            free(item);
                            free(key);
                            return 0;
                        }
                        out->prefix = grown;
                    }
                    out->prefix[out->nprefix++] = item;
                    skip_ws(&c);
                    if (c.p < c.end && *c.p == ',') {
                        c.p++;
                        continue;
                    }
                    if (c.p < c.end && *c.p == ']') {
                        c.p++;
                        break;
                    }
                    free(key);
                    return 0;
                }
            }
        } else if (!skip_value(&c)) {
            free(key);
            return 0;
        }
        free(key);
        skip_ws(&c);
        if (c.p < c.end && *c.p == ',') {
            c.p++;
            continue;
        }
        if (c.p < c.end && *c.p == '}') {
            break;
        }
        return 0;
    }
    return out->python != NULL && out->nprefix > 0;
}

/* Resolve our own path: argv[0] with '/' used as-is, else PATH search. */
static int self_path(const char *argv0, char *buf, size_t bufsz) {
    char tmp[8192];
    if (strchr(argv0, '/') != NULL) {
        snprintf(tmp, sizeof(tmp), "%s", argv0);
    } else {
        const char *path = getenv("PATH");
        if (!path) {
            path = "/usr/bin:/bin:/usr/sbin:/sbin";
        }
        tmp[0] = '\0';
        const char *p = path;
        while (1) {
            const char *colon = strchr(p, ':');
            size_t dirlen = colon ? (size_t)(colon - p) : strlen(p);
            if (dirlen > 0 && dirlen + strlen(argv0) + 2 < sizeof(tmp)) {
                memcpy(tmp, p, dirlen);
                tmp[dirlen] = '\0';
                strcat(tmp, "/");
                strcat(tmp, argv0);
                struct stat st;
                if (stat(tmp, &st) == 0) {
                    break;
                }
                tmp[0] = '\0';
            }
            if (!colon) {
                break;
            }
            p = colon + 1;
        }
        if (tmp[0] == '\0') {
            return 0;
        }
    }
    /* Resolve symlinks so the sidecar sits next to the real binary. */
    if (!realpath(tmp, buf) && bufsz > 0) {
        snprintf(buf, bufsz, "%s", tmp);
    }
    return 1;
}

static char *read_file(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) {
        return NULL;
    }
    size_t cap = 4096, len = 0;
    char *buf = (char *)malloc(cap + 1);
    if (!buf) {
        fclose(f);
        return NULL;
    }
    size_t n;
    while ((n = fread(buf + len, 1, cap - len, f)) > 0) {
        len += n;
        if (len == cap) {
            if (cap >= 65536) {
                break;
            }
            cap *= 2;
            char *grown = (char *)realloc(buf, cap + 1);
            if (!grown) {
                free(buf);
                fclose(f);
                return NULL;
            }
            buf = grown;
        }
    }
    fclose(f);
    buf[len] = '\0';
    return buf;
}

int main(int argc, char **argv) {
    char self[8192];
    if (!self_path(argv[0], self, sizeof(self))) {
        fprintf(stderr, "corral-tcc: cannot locate own binary\n");
        return 2;
    }
    size_t selflen = strlen(self);
    char *sidecar = (char *)malloc(selflen + 6);
    if (!sidecar) {
        return 2;
    }
    memcpy(sidecar, self, selflen);
    memcpy(sidecar + selflen, ".json", 6);

    char *text = read_file(sidecar);
    if (!text) {
        fprintf(stderr, "corral-tcc: missing sidecar %s (run `corral tcc wrap`)\n", sidecar);
        free(sidecar);
        return 2;
    }
    free(sidecar);

    sidecar_t sc;
    if (!parse_sidecar(text, &sc)) {
        fprintf(stderr, "corral-tcc: invalid sidecar (run `corral tcc wrap` again)\n");
        free(text);
        sidecar_free(&sc);
        return 2;
    }
    free(text);

    /* child argv = [python|prefix0] + prefix[1..] + user argv[1..] */
    int direct = sc.python[0] == '\0';
    int total = sc.nprefix + (argc - 1) + (direct ? 0 : 1);
    char **child_argv = (char **)malloc((size_t)(total + 1) * sizeof(char *));
    if (!child_argv) {
        sidecar_free(&sc);
        return 2;
    }
    int at = 0;
    if (!direct) {
        child_argv[at++] = sc.python;
    }
    for (int i = 0; i < sc.nprefix; i++) {
        child_argv[at++] = sc.prefix[i];
    }
    for (int i = 1; i < argc; i++) {
        child_argv[at++] = argv[i];
    }
    child_argv[at] = NULL;
    const char *exec_path = direct ? sc.prefix[0] : sc.python;

    install_forwarder(SIGINT);
    install_forwarder(SIGTERM);
    install_forwarder(SIGHUP);
    install_forwarder(SIGQUIT);

    pid_t pid = fork();
    if (pid < 0) {
        fprintf(stderr, "corral-tcc: fork failed: %s\n", strerror(errno));
        free(child_argv);
        sidecar_free(&sc);
        return 1;
    }
    if (pid == 0) {
        execv(exec_path, child_argv);
        fprintf(stderr, "corral-tcc: exec %s failed: %s\n", exec_path, strerror(errno));
        _exit(127);
    }
    g_child = pid;
    int status = 0;
    while (1) {
        pid_t w = waitpid(pid, &status, 0);
        if (w < 0) {
            if (errno == EINTR) {
                continue;
            }
            free(child_argv);
            sidecar_free(&sc);
            return 1;
        }
        if (WIFSTOPPED(status)) {
            continue;
        }
        break;
    }
    g_child = -1;
    free(child_argv);
    int code;
    int sig = 0;
    if (WIFEXITED(status)) {
        code = WEXITSTATUS(status);
    } else if (WIFSIGNALED(status)) {
        sig = WTERMSIG(status);
        code = 128 + sig;
    } else {
        code = 1;
    }
    sidecar_free(&sc);
    if (sig) {
        /* Die by the same signal so callers see a signal death, not exit(143). */
        signal(sig, SIG_DFL);
        kill(getpid(), sig);
    }
    return code;
}
'''


def source_tag() -> str:
    """跳板源码指纹：源码一变，旧缓存自动失效（换身份需重新授权）。"""
    return hashlib.sha256(TRAMPOLINE_C.encode("utf-8")).hexdigest()[:16]


def launcher_dir() -> str:
    """跳板安装目录：缓存目录下，与 venv 无关，Python 升级不动它。"""
    return os.path.join(cache_dir(), "tcc")


def launcher_path() -> str:
    return os.path.join(launcher_dir(), LAUNCHER_BASENAME)


def sidecar_path(path: str | None = None) -> str:
    base = path or launcher_path()
    return base + ".json"


def cached_bytes_path() -> str:
    return os.path.join(launcher_dir(), f"launcher-{source_tag()}.bin")


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    """子进程入口（测试用 monkeypatch 替换本函数即可伪造工具链）。"""
    return subprocess.run(
        argv,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise TccError(
            "TCC 跳板只在 macOS 上有意义",
            hint="Linux 上 corral 本来就是原生二进制身份，无需跳板。",
        )


def _default_target() -> tuple[str, list[str]]:
    """默认透传目标：PATH 上的 corral 脚本；没有则退回 -m 模块模式。

    返回 (python, argv_prefix)。python 为 "" 表示直接执行目标（shell 包装）。
    """
    script = shutil.which("corral")
    if script and os.path.isfile(script):
        try:
            with open(script, "rb") as handle:
                first = handle.readline(256)
        except OSError:
            first = b""
        if first.startswith(b"#!"):
            shebang = first[2:].decode("utf-8", "replace").strip().split()
            if shebang and "python" in os.path.basename(shebang[0]):
                python = shebang[0]
                if os.path.isfile(python) and os.access(python, os.X_OK):
                    return python, [script]
        # 非 Python 入口（如 pipx 的 sh 包装）：直接执行，跳板仍是父进程。
        return "", [script]
    return sys.executable, ["-m", MODULE_TARGET]


def _resolve_wrap_target(
    python: str | None, script: str | None, previous: dict | None
) -> tuple[str, list[str]]:
    if script:
        if not os.path.isfile(script):
            raise TccError(f"目标不存在：{script}")
        if os.path.realpath(script) == os.path.realpath(launcher_path()):
            raise TccError(
                "目标不能是跳板自己（会无限自指）",
                hint="用 --target 指向真正的 corral 入口，或先 unwrap。",
            )
        if python:
            return python, [script]
        default_python, _ = _default_target()
        return default_python, [script]
    if python:
        return python, ["-m", MODULE_TARGET]
    if previous and previous.get("python") is not None and previous.get("argv_prefix"):
        return str(previous["python"]), [str(a) for a in previous["argv_prefix"]]
    return _default_target()


def _compile_and_sign(dest: str, *, cc: str, sign_identity: str) -> None:
    with tempfile.TemporaryDirectory(prefix="corral-tcc-") as tmp:
        src = os.path.join(tmp, "corral-tcc.c")
        with open(src, "w", encoding="utf-8") as handle:
            handle.write(TRAMPOLINE_C)
        proc = _run([cc, "-O2", "-std=c99", "-Wall", "-o", dest + ".unsigned", src], 120.0)
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()[-500:]
            raise TccError(f"编译跳板失败：{detail}", hint=f"确认 {cc} 可用（xcode-select --install）。")
        proc = _run(
            ["codesign", "--force", "--sign", sign_identity,
             "--identifier", IDENTIFIER, dest + ".unsigned"],
            60.0,
        )
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()[-500:]
            raise TccError(f"签名跳板失败：{detail}", hint="ad-hoc 签名（-s -）不需要证书；仍失败请检查 codesign。")
        os.replace(dest + ".unsigned", dest)
    os.chmod(dest, 0o755)


def _atomic_write_bytes(path: str, data: bytes) -> None:
    tmp = f"{path}.new.{os.getpid()}"
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def _bytes_of(path: str) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def wrap(
    *,
    python: str | None = None,
    script: str | None = None,
    rebuild: bool = False,
    sign_identity: str | None = None,
    link_dir: str | None = None,
) -> dict:
    """安装/刷新跳板。幂等：字节不变时不碰二进制（cdhash 不变，授权不掉）。

    - 默认用缓存里的定稿字节（工具链漂移也不换身份）；``rebuild=True`` 才重编。
    - ``sign_identity`` 默认为 ad-hoc（``-``）；设 ``CORRAL_TCC_SIGN_IDENTITY``
      或传参可用 Developer ID（跨 rebuild 稳定，但首次仍需授权一次）。
    """
    _require_macos()
    if shutil.which("cc") is None:
        raise TccError("找不到 cc", hint="先装 Xcode 命令行工具：xcode-select --install。")
    if shutil.which("codesign") is None:
        raise TccError("找不到 codesign", hint="macOS 自带 codesign，PATH 异常请检查。")
    identity = sign_identity or os.environ.get("CORRAL_TCC_SIGN_IDENTITY") or ADHOC_IDENTITY

    directory = launcher_dir()
    os.makedirs(directory, exist_ok=True)
    dest = launcher_path()
    previous = read_sidecar()
    target_python, prefix = _resolve_wrap_target(python, script, previous)
    if target_python and not (os.path.isfile(target_python) and os.access(target_python, os.X_OK)):
        raise TccError(f"Python 不可执行：{target_python}")

    cache = cached_bytes_path()
    cache_bytes = None if rebuild else _bytes_of(cache)
    if cache_bytes is None:
        tmp_dest = dest + ".build"
        try:
            _compile_and_sign(tmp_dest, cc="cc", sign_identity=identity)
            with open(tmp_dest, "rb") as handle:
                cache_bytes = handle.read()
        finally:
            try:
                os.unlink(tmp_dest)
            except OSError:
                pass
        _atomic_write_bytes(cache, cache_bytes)
        built = True
    else:
        built = False

    installed_before = _bytes_of(dest)
    if installed_before != cache_bytes:
        _atomic_write_bytes(dest, cache_bytes)
        os.chmod(dest, 0o755)
        changed = True
    else:
        changed = False

    sidecar = {"python": target_python, "argv_prefix": prefix,
               "identifier": IDENTIFIER, "source_tag": source_tag(),
               "updated_at": time.time()}
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False,
                                     dir=directory, prefix="sidecar-") as handle:
        json.dump(sidecar, handle, ensure_ascii=False, indent=2)
        tmp_sidecar = handle.name
    os.replace(tmp_sidecar, sidecar_path(dest))

    link, link_state = _ensure_link(link_dir)
    return {
        "path": dest,
        "identifier": IDENTIFIER,
        "python": target_python,
        "argv_prefix": prefix,
        "sign_identity": identity,
        "built": built,
        "changed": changed,
        "sidecar": sidecar_path(dest),
        "cache": cache,
        "link": link,
        "link_state": link_state,
    }


def _ensure_link(link_dir: str | None) -> tuple[str | None, str]:
    """在 PATH 目录放一个 corral-tcc 软链方便调用；只建不覆盖已有文件。"""
    if link_dir is None:
        candidate = os.path.join(os.path.expanduser("~"), ".local", "bin")
        if not os.path.isdir(candidate):
            return None, "skipped"
        link_dir = candidate
    link = os.path.join(link_dir, LAUNCHER_BASENAME)
    dest = launcher_path()
    try:
        if os.path.islink(link):
            if os.path.realpath(link) == os.path.realpath(dest):
                return link, "exists"
            return link, "skipped"
        if os.path.exists(link):
            return link, "skipped"
        os.symlink(dest, link)
        return link, "created"
    except OSError:
        return link, "skipped"


def read_sidecar(path: str | None = None) -> dict | None:
    """读 sidecar；不存在或坏掉返回 None（launcher 报 2，wrap 重写可修复）。"""
    try:
        with open(sidecar_path(path), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("argv_prefix"), list) or not data["argv_prefix"]:
        return None
    if not isinstance(data.get("python"), str):
        return None
    return {"python": data["python"],
            "argv_prefix": [str(a) for a in data["argv_prefix"]]}


def unwrap() -> dict:
    """删跳板与 sidecar；定稿字节缓存保留，重装恢复同一身份。"""
    removed: list[str] = []
    for path in (launcher_path(), sidecar_path()):
        try:
            os.unlink(path)
            removed.append(path)
        except OSError:
            pass
    return {"removed": removed, "cache": cached_bytes_path(),
            "cache_kept": os.path.isfile(cached_bytes_path())}


def probe_codesign(path: str) -> dict | None:
    """codesign 探针：identifier / cdhash / team / DR；失败返回 None 永不抛错。"""
    if not path or not os.path.isfile(path):
        return None
    if shutil.which("codesign") is None:
        return None
    try:
        proc = _run(["codesign", "-dv", "--verbose=2", path], 15.0)
        text = (proc.stderr or b"").decode("utf-8", "replace")
        info: dict[str, str | bool | None] = {"path": path}
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("Identifier="):
                info["identifier"] = line.partition("=")[2]
            elif line.startswith("TeamIdentifier="):
                team = line.partition("=")[2]
                info["team"] = None if team in ("not set", "") else team
            elif line.startswith("Signature="):
                info["signature"] = line.partition("=")[2]
        info["adhoc"] = info.get("signature") == "adhoc"
        proc = _run(["codesign", "-d", "-r-", path], 15.0)
        dr = (proc.stderr or b"").decode("utf-8", "replace").strip()
        if dr:
            info["designated_requirement"] = dr if len(dr) <= 500 else dr[:500]
        if "identifier" not in info:
            return None
        return info  # type: ignore[return-value]
    except (OSError, subprocess.TimeoutExpired):
        return None


def _realpath_of(binary: str) -> dict | None:
    path = shutil.which(binary)
    if not path:
        return None
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    info = probe_codesign(real) or {"path": real}
    info["which"] = path
    info["realpath"] = real
    return info


def responsible_report(pid: int | None = None) -> dict | None:
    """launchctl 责任链探针：谁是这个 pid 的 responsible；非 darwin 返回 None。

    现代 macOS 上 ``procinfo`` 要 root：无权限时返回带 error 的字典而不是
    None，调用方能如实展示原因而非“查不到”。
    """
    if sys.platform != "darwin":
        return None
    if shutil.which("launchctl") is None:
        return None
    target = str(pid if pid is not None else os.getpid())
    try:
        proc = _run(["launchctl", "procinfo", target], 10.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace")
        reason = "needs-root" if "root" in stderr.lower() else "unavailable"
        return {"pid": target, "error": reason, "detail": stderr.strip()[-200:]}
    report: dict[str, str | None] = {"pid": target}
    for line in (proc.stdout or b"").decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("responsible pid"):
            report["responsible_pid"] = line.partition("=")[2].strip() or None
        elif line.startswith("responsible path"):
            report["responsible_path"] = line.partition("=")[2].strip() or None
    if "responsible_path" not in report:
        return {"pid": target, "error": "unavailable"}
    return report  # type: ignore[return-value]


def status() -> dict:
    """本机 TCC 身份归因总览：只读，永不抛错（diagnose 复用同一份）。"""
    data: dict = {
        "platform": sys.platform,
        "identifier": IDENTIFIER,
        "source_tag": source_tag(),
        "launcher": None,
        "python": None,
        "tmux": None,
        "responsible": None,
    }
    try:
        dest = launcher_path()
        sidecar = read_sidecar()
        installed = _bytes_of(dest)
        cached = _bytes_of(cached_bytes_path())
        data["launcher"] = {
            "path": dest,
            "exists": installed is not None,
            "matches_cache": installed is not None and cached is not None and installed == cached,
            "cache_present": cached is not None,
            "sidecar": sidecar,
            "signing": probe_codesign(dest) if installed is not None else None,
        }
        data["python"] = {
            "executable": sys.executable,
            "realpath": os.path.realpath(sys.executable),
            "signing": probe_codesign(os.path.realpath(sys.executable)),
        }
        data["tmux"] = _realpath_of("tmux")
        data["responsible"] = responsible_report()
    except Exception:  # noqa: BLE001 - status 探针永不拖垮调用方
        pass
    return data


def identity_report() -> dict | None:
    """给 diagnose 的只读字段；非 darwin 返回 None（无意义不占位）。

    绝不抛错：diagnose 有自己的 try 包裹，这里再兜一层。
    """
    try:
        if sys.platform != "darwin":
            return None
        return status()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# `corral tcc …` 命令行：status / wrap / unwrap；非 TTY 输出 JSON envelope。
# ---------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="corral tcc", description="macOS TCC 稳定身份：跳板安装与归因诊断")
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("status", help="只读：跳板/解释器/tmux/责任链归因总览")
    wrap_parser = sub.add_parser("wrap", help="编译并安装 corral-tcc 跳板（幂等）")
    wrap_parser.add_argument("--python", default=None, help="透传目标用的解释器（默认自动推导）")
    wrap_parser.add_argument("--target", default=None, help="透传目标脚本（默认 PATH 上的 corral）")
    wrap_parser.add_argument("--rebuild", action="store_true", help="重编跳板（换身份，需重新授权）")
    wrap_parser.add_argument("--sign-identity", default=None, help="签名身份（默认 ad-hoc '-'）")
    wrap_parser.add_argument("--link-dir", default=None, help="软链目录（默认 ~/.local/bin，存在才建）")
    sub.add_parser("unwrap", help="删除跳板与 sidecar（缓存保留）")
    parser.add_argument("--json", action="store_true", help="输出 JSON envelope")
    return parser


def _print_human(action: str, data: dict) -> None:
    if action == "status":
        launcher = data.get("launcher") or {}
        print(f"跳板: {launcher.get('path') or '-'}")
        print(f"  已安装: {launcher.get('exists')}, 与缓存一致: {launcher.get('matches_cache')}")
        signing = launcher.get("signing") or {}
        if signing:
            print(f"  签名: {signing.get('identifier')} / {signing.get('signature')}")
            if signing.get("designated_requirement"):
                print(f"  DR: {signing['designated_requirement']}")
        sidecar = launcher.get("sidecar")
        if sidecar:
            print(f"  透传: {sidecar.get('python') or '(直接执行)'} {' '.join(sidecar.get('argv_prefix') or [])}")
        else:
            print("  sidecar: 缺失（跑 `corral tcc wrap`）")
        python = data.get("python") or {}
        print(f"解释器: {python.get('realpath')}")
        tmux = data.get("tmux")
        if tmux:
            print(f"tmux: {tmux.get('realpath')}")
        else:
            print("tmux: 未安装")
        responsible = data.get("responsible")
        if responsible and responsible.get("responsible_path"):
            print(f"责任链: pid {responsible.get('pid')} → {responsible.get('responsible_path')}")
        elif responsible and responsible.get("error") == "needs-root":
            print("责任链: `launchctl procinfo` 需要 root，看完整归因请 sudo 跑 status")
        print("说明: 用跳板启动（`corral-tcc …`）后，corral 自身的弹窗归到跳板名下；")
        print("说明: tmux 托管层与各 agent 二进制自身的弹窗仍归它们自己（系统限制）。")
    elif action == "wrap":
        print(f"跳板: {data.get('path')}")
        if data.get("changed"):
            print("已更新二进制（身份变化 → 系统会重新弹窗一次，之后稳定）。")
        elif data.get("built"):
            print("已编译并缓存定稿字节。")
        else:
            print("二进制未动（身份不变，无需重新授权）。")
        print(f"透传: {data.get('python') or '(直接执行)'} {' '.join(data.get('argv_prefix') or [])}")
        if data.get("link") and data.get("link_state") in ("created", "exists"):
            print(f"入口: {data['link']}（用 `corral-tcc …` 代替 `corral …` 启动即生效）")
        else:
            print(f"入口: 用 {data.get('path')} 启动（加进 PATH 或做 alias 更顺手）。")
    elif action == "unwrap":
        removed = data.get("removed") or []
        print(f"已删除: {', '.join(removed) if removed else '（本来就没装）'}")
        if data.get("cache_kept"):
            print(f"缓存保留: {data.get('cache')}（重装恢复同一身份）")


def cli_main(argv: list[str] | None = None) -> int:
    """处理 `corral tcc …`；非 TTY 自动输出 JSON envelope。"""
    args_list = list(argv if argv is not None else sys.argv[1:])
    json_output = "--json" in args_list or not sys.stdout.isatty()
    # --json 是输出开关（位置随意），先摘掉再进 argparse（参考 shim 的手动预扫）。
    parse_list = [a for a in args_list if a != "--json"]
    try:
        args = _parser().parse_args(parse_list)
        action = args.action or "status"
        if action == "status":
            data = status()
        elif action == "wrap":
            data = wrap(python=args.python, script=args.target,
                        rebuild=args.rebuild, sign_identity=args.sign_identity,
                        link_dir=args.link_dir)
        else:
            data = unwrap()
        if json_output:
            print(json.dumps({"ok": True, "data": data, "error": None},
                             ensure_ascii=False, separators=(",", ":")))
        else:
            _print_human(action, data)
        return EXIT_OK
    except TccError as exc:
        if json_output:
            print(json.dumps({"ok": False, "data": None,
                              "error": {"code": "tcc_failed", "message": exc.message,
                                        "hint": exc.hint}},
                             ensure_ascii=False, separators=(",", ":")))
        else:
            print(f"失败：{exc.message}", file=sys.stderr)
            if exc.hint:
                print(f"提示：{exc.hint}", file=sys.stderr)
        return EXIT_ERROR
    except SystemExit as exc:
        return EXIT_OK if exc.code is None else int(exc.code or 0)
    except Exception as exc:  # 兜底：绝不把裸异常栈丢给用户
        if json_output:
            print(json.dumps({"ok": False, "data": None,
                              "error": {"code": "tcc_failed", "message": str(exc), "hint": ""}},
                             ensure_ascii=False, separators=(",", ":")))
        else:
            print(f"失败：{exc}", file=sys.stderr)
        return EXIT_ERROR


__all__ = [
    "ADHOC_IDENTITY", "IDENTIFIER", "LAUNCHER_BASENAME", "MODULE_TARGET",
    "TccError", "cli_main", "identity_report", "launcher_dir", "launcher_path",
    "read_sidecar", "responsible_report", "probe_codesign", "source_tag",
    "status", "unwrap", "wrap",
]
