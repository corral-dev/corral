"""Native agent questions for the phone: list the pending request, answer it natively.

A phone answer must finish the assistant's own pending question, never arrive as
an ordinary chat turn. Each runtime has one verified path (see
docs/REMOTE_KNOWLEDGE_BASE.md "Native agent questions"):

- Claude Code ``AskUserQuestion`` and Codex ``request_user_input``: drive the
  hosted TUI picker with keys (digits pick, the last row takes typed text).
- Codex ``request_user_input_async`` (0.159.2): the ``delivery=async``
  AgentMessage panel has no picker; answer with the native
  ``<send_user_message_question_reply>`` envelope (questionItemId per
  ``JSON.stringify(["request_user_input_async", itemId, index])``).
- OpenCode: its background service exposes the pending form; reply over the
  same service via ``opencode api`` (handles the service's own auth).

Other runtimes still list their questions but report ``unavailable`` on answer.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable

from corral import embed
from corral.i18n import t
from corral.remote import richmsg

KEY_RUNTIMES = frozenset({"claude", "codex"})
ANSWERABLE_RUNTIMES = KEY_RUNTIMES | {"opencode"}

_ASYNC_TOOL = "request_user_input_async"
_ASYNC_OPEN = "<send_user_message_question_reply>"
_ASYNC_CLOSE = "</send_user_message_question_reply>"

_MAX_ANSWER_CHARS = 4000
_KEY_GAP = 0.3
_PASTE_GAP = 0.45
_SETTLE_TIMEOUT = 4.0
_OPENCODE_TIMEOUT = 8.0
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b.")
_SPACE_RE = re.compile(r"\s+")

# Footer text that is on screen only while the native picker owns the keyboard.
_PICKER_MARKERS = {
    "claude": ("Enter to select", "Ready to submit your answers?"),
    # Not "None of the above": the answered history cell can repeat that label.
    "codex": ("to submit answer", "to submit all"),
}


class QuestionOutcome(Exception):
    """Terminal ``input.question`` result other than delivered."""

    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# -- listing ------------------------------------------------------------------


def pending_prompts(session: dict, messages: list[richmsg.RichMessage]) -> list[dict]:
    runtime = str(session.get("source") or "")
    if runtime == "opencode":
        return _opencode_prompts(session)
    prompts = richmsg.pending_prompts_from_messages(messages)
    if runtime not in ANSWERABLE_RUNTIMES:
        for entry in prompts:
            entry["allow_custom"] = False
    return prompts


# -- answering ----------------------------------------------------------------


def answer(
    session: dict,
    prompts: list[dict],
    request_id: str,
    raw_answers: object,
    *,
    pane_name: Callable[[], str],
) -> dict:
    """Deliver one complete answer set; returns ``{"status", "detail"}``."""
    runtime = str(session.get("source") or "")
    try:
        questions = [p for p in prompts if p.get("request_id") == request_id]
        if not request_id or not questions:
            raise QuestionOutcome("stale", t("remote.question.stale"))
        answers = _validated_answers(questions, raw_answers)
        if runtime == "opencode":
            _answer_opencode(session, request_id, questions, answers)
        elif runtime == "codex" and all(
            str(q.get("name") or "") == _ASYNC_TOOL for q in questions
        ):
            _answer_async(pane_name(), questions, answers)
        elif runtime in KEY_RUNTIMES:
            _answer_with_keys(runtime, pane_name(), questions, answers)
        else:
            raise QuestionOutcome("unavailable", t("remote.question.unsupported"))
    except QuestionOutcome as outcome:
        return {"status": outcome.status, "detail": outcome.detail}
    return {"status": "delivered"}


def _validated_answers(questions: list[dict], raw: object) -> dict[str, tuple[list[int], str]]:
    """question_id → (selected native option positions, typed text). Every question needs one."""
    if not isinstance(raw, list):
        raise QuestionOutcome("unavailable", t("remote.question.invalid"))
    by_id: dict[str, tuple[list[int], str]] = {}
    known = {str(q.get("question_id")): q for q in questions}
    for item in raw:
        if not isinstance(item, dict):
            continue
        question = known.get(str(item.get("question_id")))
        if question is None:
            raise QuestionOutcome("stale", t("remote.question.stale"))
        option_ids = [str(o.get("id")) for o in question.get("option_details") or []]
        selected: list[int] = []
        for option_id in item.get("selected") or []:
            if str(option_id) not in option_ids:
                raise QuestionOutcome("stale", t("remote.question.stale"))
            selected.append(int(option_id))
        text = str(item.get("text") or "").strip()
        if len(text) > _MAX_ANSWER_CHARS:
            raise QuestionOutcome("unavailable", t("remote.question.too_long"))
        if text and not question.get("allow_custom"):
            raise QuestionOutcome("unavailable", t("remote.question.invalid"))
        if len(selected) > 1 and not question.get("multi_select"):
            raise QuestionOutcome("unavailable", t("remote.question.invalid"))
        by_id[str(question.get("question_id"))] = (sorted(set(selected)), text)
    for question_id in known:
        picked, text = by_id.get(question_id, ([], ""))
        if not picked and not text:
            raise QuestionOutcome("unavailable", t("remote.question.incomplete"))
    return by_id


# -- TUI pickers (Claude / Codex) ----------------------------------------------


def key_plan(runtime: str, questions: list[dict], answers: dict) -> list[tuple[str, str]]:
    """Ordered ``("key", tmux_key)`` / ``("paste", text)`` steps for one picker.

    Verified on Claude Code 2.1.284 in tmux: a digit picks a single-choice option
    and moves on; on multi-choice it toggles; pasting into the last row types a
    custom answer; the review tab submits with ``1``. Codex 0.158 (tui
    request_user_input source): a digit commits; ``Down`` to "None of the above"
    + Enter opens notes; Tab on an option opens its note; Enter submits notes.
    """
    steps: list[tuple[str, str]] = []
    for question in questions:
        options = question.get("option_details") or []
        count = len(options)
        picked, text = answers[str(question.get("question_id"))]
        labels = {int(o["id"]): str(o.get("label") or "") for o in options}
        if runtime == "codex":
            if picked and text:
                steps += [("key", "Down")] * picked[0] + [("key", "Tab"), ("paste", text), ("key", "Enter")]
            elif text:
                steps += [("key", "Down")] * count + [("key", "Enter"), ("paste", text), ("key", "Enter")]
            else:
                steps.append(("key", str(picked[0] + 1)))
            continue
        if question.get("multi_select"):
            steps += [("key", str(index + 1)) for index in picked]
            if text:
                steps += [("key", "Down")] * count + [("paste", text), ("key", "Down")]
            else:
                steps += [("key", "Down")] * (count + 1)
            steps.append(("key", "Enter"))
        elif text:
            # Claude has one answer per single-choice question; keep the chosen label.
            typed = f"{labels[picked[0]]} — {text}" if picked else text
            steps += [("key", str(count + 1)), ("paste", typed), ("key", "Enter")]
        else:
            steps.append(("key", str(picked[0] + 1)))
    if runtime == "claude" and (len(questions) > 1 or any(q.get("multi_select") for q in questions)):
        steps.append(("key", "1"))  # "Submit answers" on the review tab
    return steps


def async_envelope(questions: list[dict], answers: dict) -> str:
    """Native async reply envelope (Codex 0.159.2).

    One ``<send_user_message_question_reply>`` wrapping the JSON array of
    ``{questionItemId, question, answer}`` — the desktop's ``AnsweredQuestion``
    shape (``context-fragments/src/answered_question.rs``), accepted as a
    contextual user fragment. Never plain chat: the envelope is what clears
    the async panel. Per question the answer is the typed text when present,
    else the selected option label.
    """
    replies: list[dict] = []
    for question in questions:
        question_id = str(question.get("question_id"))
        picked, text = answers[question_id]
        answer = text.strip()
        if not answer and picked:
            labels = {
                int(o["id"]): str(o.get("label") or "")
                for o in question.get("option_details") or []
            }
            answer = labels.get(picked[0], "")
        replies.append(
            {
                "questionItemId": question_id,
                "question": str(question.get("prompt") or ""),
                "answer": answer,
            }
        )
    return f"{_ASYNC_OPEN}\n{json.dumps(replies, ensure_ascii=False)}\n{_ASYNC_CLOSE}"


def _answer_async(pane: str, questions: list[dict], answers: dict) -> None:
    """Deliver the async envelope through the hosted pane.

    No picker owns the keyboard here (the async panel is a bottom pane, not
    the sync ``request_user_input`` overlay), so there is no footer/prompt
    visibility gate — staleness is decided by ``session.prompts`` before this
    is called (turn-ended or answered requests never reach here). Paste
    failures keep the phone draft (``unavailable``); a failed Enter after a
    successful paste is ``partial``.
    """
    envelope = async_envelope(questions, answers)
    if not embed.paste(pane, envelope):
        raise QuestionOutcome("unavailable", t("remote.question.not_on_screen"))
    time.sleep(_PASTE_GAP)
    if not embed.send_key(pane, "Enter"):
        raise QuestionOutcome("unavailable", t("remote.question.partial"))


def _answer_with_keys(runtime: str, name: str, questions: list[dict], answers: dict) -> None:
    screen = _pane_text(name)
    if not _picker_visible(runtime, screen) or not _question_on_screen(questions, screen):
        raise QuestionOutcome("unavailable", t("remote.question.not_on_screen"))
    sent_any = False
    for kind, value in key_plan(runtime, questions, answers):
        ok = embed.paste(name, value) if kind == "paste" else embed.send_key(name, value)
        if not ok:
            detail = "remote.question.partial" if sent_any else "remote.question.not_on_screen"
            raise QuestionOutcome("unavailable", t(detail))
        sent_any = True
        time.sleep(_PASTE_GAP if kind == "paste" else _KEY_GAP)
    deadline = time.monotonic() + _SETTLE_TIMEOUT
    while time.monotonic() < deadline:
        if not _picker_visible(runtime, _pane_text(name)):
            return
        time.sleep(0.25)
    raise QuestionOutcome("unavailable", t("remote.question.partial"))


def _pane_text(name: str) -> str:
    return _ANSI_RE.sub("", embed.capture(name, 0, 0) or "")


def _picker_visible(runtime: str, screen: str) -> bool:
    return any(marker in screen for marker in _PICKER_MARKERS.get(runtime, ()))


def _question_on_screen(questions: list[dict], screen: str) -> bool:
    """The first question's prompt (or first option) must be the picker on screen."""
    flat = _SPACE_RE.sub("", screen)
    first = questions[0]
    probes = [str(first.get("prompt") or "")]
    probes += [str(o.get("label") or "") for o in (first.get("option_details") or [])[:1]]
    for probe in probes:
        needle = _SPACE_RE.sub("", probe)[:24]
        if needle and needle in flat:
            return True
    return False


# -- OpenCode forms -------------------------------------------------------------


def _opencode_api(*args: str) -> tuple[int, str]:
    try:
        done = subprocess.run(
            ["opencode", "api", *args],
            capture_output=True,
            text=True,
            timeout=_OPENCODE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return done.returncode, (done.stdout or done.stderr or "").strip()


def _opencode_forms(session_id: str) -> list[dict]:
    code, out = _opencode_api("GET", f"/api/session/{session_id}/form")
    if code != 0:
        return []
    try:
        data = json.loads(out).get("data")
    except (ValueError, AttributeError):
        return []
    return [form for form in data if isinstance(form, dict)] if isinstance(data, list) else []


def _opencode_prompts(session: dict) -> list[dict]:
    session_id = str(session.get("id") or "")
    if not session_id or not session.get("live"):
        return []
    for form in _opencode_forms(session_id):
        metadata = form.get("metadata") if isinstance(form.get("metadata"), dict) else {}
        if metadata.get("kind") != "question":
            continue
        questions = _opencode_questions(form.get("fields"))
        if questions:
            return richmsg.prompt_entries(
                request_id=str(form.get("id") or ""),
                name="question",
                questions=questions,
            )
    return []


def _opencode_questions(fields: object) -> list[dict]:
    questions: list[dict] = []
    for field in fields if isinstance(fields, list) else []:
        if not isinstance(field, dict) or field.get("type") not in ("string", "multiselect"):
            return []  # never half-answer a form with a field type we cannot fill
        options = [
            {
                "id": str(index),
                "label": str(option.get("label") or option.get("value") or ""),
                "value": option.get("value"),
                **({"description": str(option["description"])} if option.get("description") else {}),
            }
            for index, option in enumerate(field.get("options") or [])
            if isinstance(option, dict)
        ]
        prompt = str(field.get("description") or field.get("title") or "")
        questions.append(
            {
                "id": str(field.get("key") or len(questions)),
                "prompt": prompt,
                "header": str(field.get("title") or "") if field.get("title") != prompt else "",
                "multi_select": field.get("type") == "multiselect",
                "allow_custom": field.get("custom") is not False,
                "options": options,
            }
        )
    return questions


def opencode_answer_payload(questions: list[dict], answers: dict) -> dict:
    payload: dict = {}
    for question in questions:
        question_id = str(question.get("question_id"))
        picked, text = answers[question_id]
        values = {int(o["id"]): o.get("value") or o.get("label") for o in question.get("option_details") or []}
        chosen = [str(values[index]) for index in picked]
        if question.get("multi_select"):
            payload[question_id] = chosen + ([text] if text else [])
        elif text:
            payload[question_id] = f"{chosen[0]} — {text}" if chosen else text
        else:
            payload[question_id] = chosen[0]
    return {"answer": payload}


def _answer_opencode(session: dict, form_id: str, questions: list[dict], answers: dict) -> None:
    session_id = str(session.get("id") or "")
    if not any(str(form.get("id")) == form_id for form in _opencode_forms(session_id)):
        raise QuestionOutcome("stale", t("remote.question.stale"))
    body = json.dumps(opencode_answer_payload(questions, answers), ensure_ascii=False)
    code, out = _opencode_api(
        "POST", f"/api/session/{session_id}/form/{form_id}/reply", "-d", body
    )
    if code != 0:
        settled = "Settled" in out or "NotFound" in out
        raise QuestionOutcome(
            "stale" if settled else "unavailable",
            t("remote.question.stale") if settled else t("remote.question.opencode_failed"),
        )
