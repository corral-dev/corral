"""Durable delegation runs: intent, messages, events and audit in one SQLite file.

Reads never create or repair storage. Every write commits its audit row
(actor, action, target, timestamp) in the same transaction as the change.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

TERMINAL = frozenset({"completed", "failed", "interrupted"})
# A run stops needing supervision once its outcome is final or unknowable.
SETTLED = TERMINAL | {"unknown"}
SUPERVISOR = "corral-supervisor"

EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_CONFLICT = 5
EXIT_TIMEOUT = 6


class ControlError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = EXIT_ERROR, *,
                 hint: str | None = None, next_commands: list[str] | None = None,
                 details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.exit_code = code, message, exit_code
        self.hint, self.next_commands, self.details = hint, next_commands or [], details


def control_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "corral" / "control"


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def run_version(record: dict) -> str:
    """Fingerprint only what a follow-up write depends on, so progress does not churn it."""
    basis = {"execution": record.get("execution"), "delivery": record.get("delivery"),
             "questions": sorted(str(q.get("request_id")) for q in record.get("questions") or [])}
    return "v2:" + digest(basis)[:16]


SCHEMA_VERSION = 1
MAX_ACTIVE_RUNS = 8


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
    id TEXT PRIMARY KEY, actor TEXT NOT NULL, request_key TEXT NOT NULL,
    digest TEXT NOT NULL, spec TEXT NOT NULL, data TEXT NOT NULL,
    created_at REAL NOT NULL, UNIQUE(request_key));
CREATE TABLE IF NOT EXISTS events(
    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
    identity TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(run_id, identity));
CREATE TABLE IF NOT EXISTS messages(
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, actor TEXT NOT NULL, request_key TEXT NOT NULL,
    digest TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL, detail TEXT,
    created_at REAL NOT NULL, UNIQUE(run_id, request_key));
CREATE TABLE IF NOT EXISTS supervisors(
    run_id TEXT PRIMARY KEY, pid INTEGER NOT NULL, started_at REAL NOT NULL, heartbeat_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
    seq INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
    target TEXT NOT NULL, timestamp REAL NOT NULL);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq);
"""


class RunStore:
    def __init__(self, root: Path | None = None):
        self.root = root or control_home()
        self.path = self.root / "runs.sqlite3"

    def exists(self) -> bool:
        return self.path.is_file()

    @contextmanager
    def connection(self, *, write: bool = False):
        if write:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.is_symlink():
                raise ControlError("unsafe_storage", "The control database must not be a symlink.", EXIT_CONFLICT)
            conn = sqlite3.connect(self.path, timeout=15)
            os.chmod(self.path, 0o600)
        else:
            if not self.path.is_file():
                raise ControlError("not_found", "No delegation runs exist yet.", EXIT_NOT_FOUND)
            conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.executescript(_SCHEMA)
                current = conn.execute("PRAGMA user_version").fetchone()[0]
                if current > SCHEMA_VERSION:
                    raise ControlError("store_too_new", "Run storage was written by a newer Corral; upgrade first.",
                                       EXIT_CONFLICT)
                if current < SCHEMA_VERSION:
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        finally:
            conn.close()

    # -- primitives (caller holds the connection) ---------------------------

    @staticmethod
    def _audit(conn, actor: str, action: str, target: str) -> None:
        conn.execute("INSERT INTO audit(actor,action,target,timestamp) VALUES(?,?,?,?)",
                     (actor, action, target, time.time()))

    @staticmethod
    def _row(conn, run_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ControlError("not_found", f"Unknown run: {run_id}", EXIT_NOT_FOUND)
        return row

    def _append(self, conn, run_id: str, identity: str, body: dict) -> bool:
        cur = conn.execute("INSERT OR IGNORE INTO events(run_id,identity,body) VALUES(?,?,?)",
                           (run_id, identity, json.dumps({"at": time.time(), **body}, ensure_ascii=False)))
        return bool(cur.rowcount)

    @staticmethod
    def _decode(row) -> dict:
        data = json.loads(row["data"])
        data["version"] = run_version(data)
        return data

    # -- runs -----------------------------------------------------------------

    def get(self, run_id: str, *, private: bool = False) -> dict:
        with self.connection() as conn:
            row = self._row(conn, run_id)
            sup = conn.execute("SELECT * FROM supervisors WHERE run_id=?", (run_id,)).fetchone()
        data = self._decode(row)
        data["supervisor"] = dict(sup) if sup else None
        if private:
            data["spec"] = json.loads(row["spec"])
        return data

    def recent(self, limit: int, actor: str | None = None) -> list[dict]:
        query = "SELECT * FROM runs" + (" WHERE actor=?" if actor else "") + " ORDER BY created_at DESC LIMIT ?"
        params = (actor, limit) if actor else (limit,)
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._decode(r) for r in rows]

    def find(self, key: str) -> dict | None:
        if not self.exists():
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM runs WHERE request_key=?", (key,)).fetchone()
        return {**self._decode(row), "_digest": row["digest"]} if row else None

    def unsettled(self) -> list[dict]:
        if not self.exists():
            return []
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at").fetchall()
        return [d for d in (self._decode(r) for r in rows) if d.get("execution") not in SETTLED]

    def for_target(self, session_ref: str) -> list[dict]:
        return [r for r in self.unsettled() if r.get("session_ref") == session_ref]

    def create(self, actor: str, key: str, spec: dict, initial: dict) -> tuple[dict, bool]:
        """Insert one run per (actor, key); the same key with another spec conflicts."""
        fingerprint = digest(spec)
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT * FROM runs WHERE request_key=?", (key,)).fetchone()
            if row is not None:
                if row["digest"] != fingerprint:
                    raise ControlError(
                        "idempotency_conflict", "This idempotency key already identifies a different request.",
                        EXIT_CONFLICT, hint="Reuse the key only for an identical retry; pick a new key for new work.",
                    )
                return self._decode(row), False
            run_id = "run_" + uuid.uuid4().hex
            now = time.time()
            data = {"run_id": run_id, "revision": 1, "actor": actor, "created_at": now, "updated_at": now,
                    **initial}
            conn.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
                         (run_id, actor, key, fingerprint, json.dumps(spec, ensure_ascii=False),
                          json.dumps(data, ensure_ascii=False), now))
            self._audit(conn, actor, "run.create", run_id)
            self._append(conn, run_id, "state:1", {"kind": "state", "delivery": data.get("delivery"),
                                                  "execution": data.get("execution")})
        data["version"] = run_version(data)
        return data, True

    def update(self, run_id: str, changes: dict, *, actor: str = SUPERVISOR, expected: str | None = None,
               allow_settled: bool = False) -> dict:
        with self.connection(write=True) as conn:
            data = json.loads(self._row(conn, run_id)["data"])
            if expected is not None and run_version(data) != expected:
                raise ControlError("conflict", "The run changed; read its current version before writing.",
                                   EXIT_CONFLICT)
            if data.get("execution") in TERMINAL and not allow_settled:
                raise ControlError("terminal_run", "A finished run is immutable; submit a new run instead.",
                                   EXIT_CONFLICT)
            changed = {k: v for k, v in changes.items() if data.get(k) != v}
            if not changed:
                return {**data, "version": run_version(data)}
            data.update(changed)
            data["revision"] += 1
            data["updated_at"] = time.time()
            data["version"] = run_version(data)
            conn.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), run_id))
            self._audit(conn, actor, "run.update", run_id)
            visible = {k: v for k, v in changed.items() if k in {"delivery", "execution", "error", "session_ref",
                                                                 "attribution", "observed_model"}}
            if "questions" in changed:
                visible["questions"] = len(changed["questions"] or [])
            if visible:
                self._append(conn, run_id, f"state:{data['revision']}", {"kind": "state", **visible})
        data["version"] = run_version(data)
        return data

    # -- events ---------------------------------------------------------------

    def append(self, run_id: str, identity: str, body: dict) -> bool:
        with self.connection(write=True) as conn:
            self._row(conn, run_id)
            added = self._append(conn, run_id, identity, body)
            if added:
                self._audit(conn, SUPERVISOR, "event.append", run_id)
        return added

    def events(self, run_id: str, after: int = 0, limit: int = 50) -> dict:
        with self.connection() as conn:
            self._row(conn, run_id)
            rows = conn.execute("SELECT seq, body FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                                (run_id, after, limit + 1)).fetchall()
        page = rows[:limit]
        return {"events": [{"seq": r["seq"], **json.loads(r["body"])} for r in page],
                "next_cursor": page[-1]["seq"] if page else after, "has_more": len(rows) > limit}

    # -- follow-up messages (steer / answer / interrupt) ----------------------

    def enqueue(self, run_id: str, actor: str, key: str, body: dict, expected: str) -> tuple[dict, bool]:
        fingerprint = digest(body)
        with self.connection(write=True) as conn:
            row = self._row(conn, run_id)
            old = conn.execute("SELECT * FROM messages WHERE run_id=? AND request_key=?",
                               (run_id, key)).fetchone()
            if old is not None:
                if old["digest"] != fingerprint:
                    raise ControlError("idempotency_conflict", "This message key was used for different input.",
                                       EXIT_CONFLICT)
                return self._message(old), False
            data = json.loads(row["data"])
            if run_version(data) != expected:
                raise ControlError("conflict", "The run changed; read its current version before writing.",
                                   EXIT_CONFLICT)
            if data.get("execution") in SETTLED:
                raise ControlError("terminal_run", "This run has settled; submit a new run on the same session.",
                                   EXIT_CONFLICT)
            ident = "msg_" + uuid.uuid4().hex[:16]
            conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                         (ident, run_id, actor, key, fingerprint, json.dumps(body, ensure_ascii=False),
                          "queued", None, time.time()))
            self._audit(conn, actor, f"message.{body['action']}", ident)
            self._append(conn, run_id, ident, {"kind": "message", "message_id": ident,
                                               "action": body["action"], "state": "queued"})
            new = conn.execute("SELECT * FROM messages WHERE id=?", (ident,)).fetchone()
        return self._message(new), True

    @staticmethod
    def _message(row) -> dict:
        return {"message_id": row["id"], "run_id": row["run_id"], "action": json.loads(row["body"])["action"],
                "state": row["state"], "detail": row["detail"], "created_at": row["created_at"]}

    def pending_messages(self, run_id: str) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM messages WHERE run_id=? AND state='queued' ORDER BY created_at",
                                (run_id,)).fetchall()
        return [{"id": r["id"], "body": json.loads(r["body"])} for r in rows]

    def messages(self, run_id: str) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM messages WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()
        return [self._message(r) for r in rows]

    def message_state(self, ident: str, state: str, detail: str | None = None) -> None:
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT run_id FROM messages WHERE id=?", (ident,)).fetchone()
            conn.execute("UPDATE messages SET state=?, detail=? WHERE id=?", (state, detail, ident))
            self._audit(conn, SUPERVISOR, f"message.{state}", ident)
            if row is not None:
                self._append(conn, row["run_id"], f"{ident}:{state}",
                             {"kind": "message", "message_id": ident, "state": state, "detail": detail})

    # -- supervision ----------------------------------------------------------

    def heartbeat(self, run_id: str, *, started: bool = False) -> None:
        now = time.time()
        with self.connection(write=True) as conn:
            if started:
                conn.execute("INSERT OR REPLACE INTO supervisors VALUES(?,?,?,?)", (run_id, os.getpid(), now, now))
                self._audit(conn, SUPERVISOR, "supervisor.start", run_id)
            else:
                conn.execute("UPDATE supervisors SET heartbeat_at=? WHERE run_id=? AND pid=?",
                             (now, run_id, os.getpid()))

    def active_hosts(self) -> set[str] | None:
        """Hosted terminals with unsettled runs; ``None`` when the store is unreadable."""
        if not self.exists():
            return set()
        try:
            with self.connection() as conn:
                rows = conn.execute("SELECT data FROM runs").fetchall()
        except (sqlite3.Error, ControlError):
            return None
        names = set()
        for row in rows:
            data = json.loads(row["data"])
            if data.get("execution") not in SETTLED and data.get("host"):
                names.add(str(data["host"]))
        return names
