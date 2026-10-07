"""corral：终端会话接力工具。

包顶层保持历史兼容导出，但全部按需加载，避免 ``corral --version`` 和机器命令
为未使用的 Textual、网络、tmux 与扫描模块支付导入成本。
"""

from __future__ import annotations

import importlib
import os
import sys as sys

# 关掉 Textual 默认开启的 Kitty 键盘协议，必须在任何 `import textual` 之前生效。
# 放在包顶层而不是 `cli.py`：`textual.constants` 是在导入时一次性读环境变量定死的，
# 只要有任何一条路径先 import textual 再 import corral.cli（测试套件、第三方嵌入、
# 只 `import corral` 的脚本都会），这道保护就整个失效——CI 上 5 个 Python 版本的
# 回归用例全挂正是这么来的。包顶层是唯一「任何用法都必经」的位置。
os.environ.setdefault("TEXTUAL_DISABLE_KITTY_KEY", "1")

# SessKit owns session parsers; wire Corral's performance cache into SessKit.
def _wire_sesskit_cache() -> None:
    try:
        from sesskit.cache import set_cache

        from corral.cache import get_cache

        set_cache(get_cache())
    except Exception:
        pass


_wire_sesskit_cache()

__version__ = "0.24.271"

_MODULE_EXPORTS = {
    "embed", "keepalive", "liveness", "titles", "updater", "split_layout", "observe", "theme", "search",
}
_STANDARD_MODULE_EXPORTS = {"shutil"}
_STANDARD_SYMBOL_EXPORTS = {"datetime": ("datetime", "datetime")}
_SYMBOL_EXPORTS = {
    "_DirectLaunch": ("corral.cli", "_DirectLaunch"),
    "_dispatch_direct_launch": ("corral.cli", "_dispatch_direct_launch"),
    "_finish_self_update": ("corral.cli", "_finish_self_update"),
    "_launch": ("corral.cli", "_launch"),
    "_require_tmux": ("corral.cli", "_require_tmux"),
    "_restart_process": ("corral.cli", "_restart_process"),
    "_spawn_title_daemon": ("corral.cli", "_spawn_title_daemon"),
    "main": ("corral.bootstrap", "main"),
    "SPINNER_FRAMES": ("corral.display", "SPINNER_FRAMES"),
    "UNKNOWN_PROJECT_LABEL": ("corral.projects", "UNKNOWN_PROJECT_LABEL"),
    "_disambiguate_labels": ("corral.projects", "disambiguate_labels"),
    "_filter_sessions": ("corral.display", "_filter_sessions"),
    "_filter_sessions_by_query": ("corral.display", "_filter_sessions_by_query"),
    "_fit_cell": ("corral.textutil", "fit_cell"),
    "_fit_cell_right": ("corral.textutil", "fit_cell_right"),
    "_format_relative_time": ("corral.display", "_format_relative_time"),
    "_fuzzy_match": ("corral.projects", "fuzzy_match"),
    "_time_brightness_tier": ("corral.display", "_time_brightness_tier"),
    "TIME_BRIGHTNESS_TIERS": ("corral.display", "TIME_BRIGHTNESS_TIERS"),
    "TODAY_SECONDS": ("corral.display", "TODAY_SECONDS"),
    "_normalize_cwd": ("corral.projects", "normalize_cwd"),
    "_preview_blocks": ("corral.display", "_preview_blocks"),
    "_project_groups": ("corral.display", "_project_groups"),
    "_text_width": ("corral.textutil", "text_width"),
    "_wrap_preview_text": ("corral.textutil", "wrap_preview_text"),
    "ConversationMessage": ("corral.models", "ConversationMessage"),
    "LaunchPlan": ("corral.models", "LaunchPlan"),
    "LaunchRequest": ("corral.models", "LaunchRequest"),
    "NewSessionRequest": ("corral.models", "NewSessionRequest"),
    "format_message_time": ("corral.models", "format_message_time"),
    "session_key": ("corral.models", "session_key"),
    "_log_embed_error": ("corral.observe", "log_embed_error"),
    "LaunchError": ("corral.runtime", "LaunchError"),
    "RuntimeRegistry": ("corral.runtime", "RuntimeRegistry"),
    "default_registry": ("corral.runtime", "default_registry"),
    "execute_launch": ("corral.runtime", "execute_launch"),
    "usable_cwd": ("corral.runtime", "usable_cwd"),
    "SessionStore": ("corral.store", "SessionStore"),
    "_new_session_cwd": ("corral.store", "_new_session_cwd"),
    "RUNTIME_LABEL_STYLES": ("corral.theme", "RUNTIME_LABEL_STYLES"),
    "_background_channels": ("corral.theme", "_background_channels"),
    "_background_is_light": ("corral.theme", "_background_is_light"),
    "_background_rgb": ("corral.theme", "_background_rgb"),
    "_probe_osc_colours": ("corral.theme", "_probe_osc_colours"),
    "runtime_label_style": ("corral.theme", "runtime_label_style"),
}


def __getattr__(name: str):
    if name in _STANDARD_SYMBOL_EXPORTS:
        module_name, symbol_name = _STANDARD_SYMBOL_EXPORTS[name]
        value = getattr(importlib.import_module(module_name), symbol_name)
    elif name in _STANDARD_MODULE_EXPORTS:
        value = importlib.import_module(name)
    elif name in _MODULE_EXPORTS:
        value = importlib.import_module(f"corral.{name}")
    else:
        target = _SYMBOL_EXPORTS.get(name)
        if target is None:
            raise AttributeError(f"module 'corral' has no attribute {name!r}")
        value = getattr(importlib.import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(
        set(globals()) | _MODULE_EXPORTS | _STANDARD_MODULE_EXPORTS
        | set(_STANDARD_SYMBOL_EXPORTS) | set(_SYMBOL_EXPORTS)
    )
