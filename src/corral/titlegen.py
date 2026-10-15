"""Session title transports: configured gateway, otherwise existing local Agents.

Config file (optional): ``~/.config/corral/llm-gateway.json``

Keys:
- ``base_url`` — explicitly configured OpenAI-compatible root
- ``api_key`` — gateway virtual key (required for generation; never logged)
- ``model`` — optional gateway alias override; when omitted the live
  ``/v1/models`` catalog is queried and ``budget-chat`` is preferred when present

Environment overrides: ``CORRAL_LLM_GATEWAY_URL``, ``CORRAL_LLM_GATEWAY_KEY``,
``CORRAL_TITLE_MODEL``, ``CORRAL_LLM_GATEWAY_CONFIG``.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

ENV_GATEWAY_URL = "CORRAL_LLM_GATEWAY_URL"
ENV_GATEWAY_KEY = "CORRAL_LLM_GATEWAY_KEY"
ENV_MODEL = "CORRAL_TITLE_MODEL"
ENV_CONFIG = "CORRAL_LLM_GATEWAY_CONFIG"
DEFAULT_GATEWAY_URL = ""
DEFAULT_CONFIG_PATH = Path("~/.config/corral/llm-gateway.json").expanduser()
PREFERRED_MODEL_ALIAS = "budget-chat"
_MODEL_CATALOG_TTL_SECONDS = 60.0

# (fetched_at, chosen_alias); cleared by tests via clear_model_cache().
_model_cache: tuple[float, str] | None = None


def _setting(env_name: str, config_name: str, default: str = "") -> str:
    """Read an environment setting first, then the durable user config."""
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    try:
        config = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    value = config.get(config_name, "") if isinstance(config, dict) else ""
    return str(value).strip() or default


def _config_path() -> Path:
    configured = os.environ.get(ENV_CONFIG, "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_CONFIG_PATH


def gateway_url() -> str:
    return _setting(ENV_GATEWAY_URL, "base_url", DEFAULT_GATEWAY_URL).rstrip("/")


def gateway_key() -> str | None:
    key = _setting(ENV_GATEWAY_KEY, "api_key")
    return key or None


def configured_title_model() -> str | None:
    """Explicit model override from env or config; does not consult the catalog."""
    # CORRAL_TITLE_MODEL is intentionally the only model override. Provider
    # names and fallback mapping remain entirely inside the gateway.
    return _setting(ENV_MODEL, "model") or None


def title_model() -> str | None:
    """Resolve the alias used for the next title request."""
    return configured_title_model() or _live_preferred_model()


def clear_model_cache() -> None:
    """Drop the short-lived live-catalog preference (tests / config changes)."""
    global _model_cache
    _model_cache = None


def _live_preferred_model() -> str | None:
    """Pick a live gateway alias; prefer budget-chat; never invent names."""
    global _model_cache
    now = time.monotonic()
    if _model_cache is not None and now - _model_cache[0] < _MODEL_CATALOG_TTL_SECONDS:
        return _model_cache[1]
    aliases = _fetch_model_aliases()
    if not aliases:
        return None
    chosen = PREFERRED_MODEL_ALIAS if PREFERRED_MODEL_ALIAS in aliases else aliases[0]
    _model_cache = (now, chosen)
    return chosen


def _fetch_model_aliases() -> list[str]:
    request = urllib.request.Request(
        f"{gateway_url()}/models",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read(1_000_000).decode("utf-8"))
    except (OSError, TimeoutError, ValueError, UnicodeError, urllib.error.HTTPError):
        return []
    if not isinstance(body, dict):
        return []
    data = body.get("data")
    if not isinstance(data, list):
        return []
    aliases: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        alias = item.get("id")
        if isinstance(alias, str) and alias.strip():
            aliases.append(alias.strip())
    return aliases


class TitleGenerator(ABC):
    """Compatibility interface consumed by the existing title pipeline."""

    id = "gateway"
    executable = ""

    def is_available(self) -> bool:
        # Both the destination and virtual key must be explicitly configured.
        # The model alias can be resolved from the live catalog at request time.
        return bool(gateway_url()) and gateway_key() is not None

    @abstractmethod
    def generate(self, prompt: str, timeout: int) -> str | None:
        """Return the gateway assistant text, or None on any failure."""


class GatewayTitleGenerator(TitleGenerator):
    """Submit one bounded request to the shared gateway."""

    def generate(self, prompt: str, timeout: int) -> str | None:
        if not self.is_available():
            return None
        key = gateway_key()
        model = title_model()
        if not key or not model:
            return None
        payload = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                # 2048, not 256: openrouter-auto can route to a reasoning model
                # whose thinking eats the whole output budget (finish=length with
                # content=null at 256; single-session prompts still overflow 1024
                # on unlucky samples with 4000+ chars of thinking; verified
                # 2026-09-23). Cost stays ~$0.0006/call.
                "max_tokens": 2048,
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{gateway_url()}/chat/completions",
            data=payload,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read(1_000_000).decode("utf-8"))
        except (OSError, TimeoutError, ValueError, UnicodeError, urllib.error.HTTPError):
            return None
        return _assistant_text(body)


def _assistant_text(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    # Some routed models (e.g. via openrouter-auto) return the OpenAI-style
    # content-block list instead of a plain string. Join text blocks; anything
    # else stays a transport failure so the batch retries later.
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        text = "".join(parts)
        return text or None
    return None


_GENERATOR = GatewayTitleGenerator()


def _job_env() -> dict[str, str]:
    """Keep authentication, but never let a derived job claim its parent's pane."""
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith(("CORRAL_", "PICKUP_", "SC_")) and any(
            word in key for word in ("SESSION", "RUNTIME", "CLAIM", "INSTANCE")
        ):
            env.pop(key)
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "PI_CODING_AGENT_SESSION_DIR"):
        env.pop(key, None)
    env.update(CORRAL_SHIM_ACTIVE="1", NO_OPEN_BROWSER="1", PWD="")
    return env


def _run_local(argv: list[str], prompt: str, timeout: int, cwd: Path, env: dict[str, str]) -> str | None:
    """Bound output and lifetime; a timed-out CLI must not leave its server alive."""
    env = {**env, "PWD": str(cwd)}
    with tempfile.TemporaryFile() as output:
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", cwd=cwd, env=env, start_new_session=True,
            )
        except OSError:
            return None
        try:
            proc.communicate(prompt, timeout=timeout)
            if proc.returncode != 0:
                return None
            output.seek(0)
            raw = output.read(1_000_001)
            return raw.decode("utf-8") if len(raw) <= 1_000_000 else None
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            return None
        finally:
            # Our own process group only, including a standalone server whose
            # launcher exited before the server. Never use process-name killing.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()


class LocalTitleGenerator(TitleGenerator):
    """One runtime's stateless invocation, independent of session launch adapters."""

    def __init__(self, runtime: str, executable: str, help_args: tuple[str, ...], flags: tuple[str, ...]):
        self.id = runtime
        self.executable = executable
        self.help_args = help_args
        self.flags = flags
        self._capability: tuple[str, float, bool] | None = None

    def is_available(self) -> bool:
        # This also runs while resolving local fallback titles on first paint.
        # Capability probes belong only to the detached generation worker.
        return bool(self._executable())

    def _executable(self) -> str | None:
        executable = shutil.which(self.executable)
        if not executable and self.id == "cursor":
            executable = shutil.which("cursor-agent")
        return executable

    def _supports_invocation(self, cwd: Path) -> bool:
        executable = self._executable()
        if not executable:
            return False
        now = time.monotonic()
        if self._capability and self._capability[0] == executable and now - self._capability[1] < 60:
            return self._capability[2]
        # Help is local and never initiates a login or a model call.
        help_text = _run_local([executable, *self.help_args, "--help"], "", 5, cwd, _job_env())
        compatible = help_text is not None and all(flag in help_text for flag in self.flags)
        self._capability = (executable, now, compatible)
        return compatible

    def generate(self, prompt: str, timeout: int) -> str | None:
        root = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "corral" / "title-worker"
        try:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            (root / ".sesskit-ignore").touch(mode=0o600, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=f"{self.id}-", dir=root) as directory:
                cwd = Path(directory)
                if not self._supports_invocation(cwd):
                    return None
                env = _job_env()
                argv = self._command(cwd, env)
                raw = _run_local(argv, prompt, timeout, cwd, env)
                if raw is None:
                    return None
                if self.id == "codex":
                    output = cwd / "answer.txt"
                    if not output.exists() or output.stat().st_size > 1_000_000:
                        return None
                    return output.read_text(encoding="utf-8")
                if self.id == "opencode":
                    # JSONL separates model text from server/status messages.
                    parts = []
                    for line in raw.splitlines():
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        part = event.get("part") if isinstance(event, dict) else None
                        if isinstance(part, dict) and event.get("type") == "text":
                            text = part.get("text")
                            if isinstance(text, str):
                                parts.append(text)
                    return "".join(parts) or None
                return raw
        except (OSError, UnicodeError, ValueError):
            return None

    def _command(self, cwd: Path, env: dict[str, str]) -> list[str]:
        executable = self._executable() or self.executable
        if self.id == "claude":
            return [executable, "-p", "--no-session-persistence", "--tools", "",
                    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                    "--disable-slash-commands", "--settings", '{"disableAllHooks":true}',
                    "--dangerously-skip-permissions"]
        if self.id == "codex":
            return [executable, "-a", "never", "exec", "--skip-git-repo-check", "--ephemeral",
                    "--sandbox", "read-only", "--color", "never",
                    "--disable", "shell_tool", "--disable", "unified_exec",
                    "--disable", "multi_agent", "-c", 'web_search="disabled"',
                    "-c", "project_doc_max_bytes=0", "-o", str(cwd / "answer.txt"), "-"]
        if self.id == "pi":
            return [executable, "--approve", "--no-session", "--no-tools",
                    "--no-skills", "--no-prompt-templates", "--no-context-files", "--print"]
        if self.id == "opencode":
            env["OPENCODE_DB"] = str(cwd / "opencode.db")
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps({
                "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
            })
            return [executable, "run", "--standalone", "--auto", "--format", "json"]
        # Keep CURSOR_CONFIG_DIR unchanged: login and data are separate. The
        # private data directory contains only this job and is removed afterward.
        env["CURSOR_DATA_DIR"] = str(cwd / "data")
        config_dir = cwd / ".cursor"
        config_dir.mkdir()
        (config_dir / "cli.json").write_text(json.dumps({"permissions": {"allow": [], "deny": [
            "Shell(*)", "Read(**)", "Write(**)", "WebFetch(*)", "Mcp(*:*)",
        ]}}), encoding="utf-8")
        return [executable, "-p", "--mode", "ask", "--output-format", "text", "--trust", "--force"]


_LOCAL_GENERATORS = (
    LocalTitleGenerator("claude", "claude", (), ("--no-session-persistence", "--tools", "--strict-mcp-config")),
    LocalTitleGenerator("codex", "codex", ("exec",), ("--ephemeral", "--output-last-message")),
    LocalTitleGenerator("pi", "pi", (), ("--no-session", "--no-tools", "--no-context-files")),
    LocalTitleGenerator("opencode", "opencode", ("run",), ("--standalone", "--format")),
    LocalTitleGenerator("cursor", "agent", (), ("Cursor", "--mode", "--workspace")),
)


def available_generators() -> tuple[TitleGenerator, ...]:
    """A configured gateway owns its route; otherwise reuse local Agent login."""
    if _GENERATOR.is_available():
        return (_GENERATOR,)
    return tuple(generator for generator in _LOCAL_GENERATORS if generator.is_available())


def resolve_generator() -> TitleGenerator | None:
    """Compatibility helper for callers expecting one generator."""
    generators = available_generators()
    return generators[0] if generators else None
