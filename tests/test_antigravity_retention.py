"""Native Antigravity discovery must replace remembered empty hosted cards."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from corral import liveness
from corral.attention import AttentionStore
from corral.runtime import RuntimeRegistry
from corral.runtime.antigravity import AntigravityRuntime
from corral.scan import antigravity as parser
from corral.store import SessionStore


class AntigravityRetentionTests(unittest.TestCase):
    def test_remembered_placeholders_discover_promote_and_export_exact_histories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            presence = root / "presence"
            presence.mkdir()
            summaries = {}
            hosts = []
            descriptors = []
            paths = {}
            now = time.time()
            for index, cid in enumerate(("native-a", "native-b")):
                stamp = now - 20 - index
                created = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
                path = root / "brain" / cid / ".system_generated" / "logs" / "transcript_full.jsonl"
                path.parent.mkdir(parents=True)
                rows = [
                    {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT",
                     "status": "DONE", "content": f"Repair retention task {index}", "created_at": created},
                    {"step_index": 1, "type": "PLANNER_RESPONSE", "status": "RUNNING",
                     "content": "Inspecting the task", "created_at": created},
                ]
                path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                os.utime(path, (stamp, stamp))
                paths[cid] = str(path)
                summaries[cid] = {
                    "title": f"Retention repair {index}", "root": str(root),
                    "workspace_uris": json.dumps([project.as_uri()]),
                    "last_modified_time": created, "status": "CASCADE_RUN_STATUS_RUNNING",
                }
                pid = 10001 + index
                ident = f"temporary-{index}"
                name = f"corral-antigravity-{ident}"
                lock = presence / f"{cid}.lock"
                lock.touch()
                descriptors.append((pid, "u", str(lock)))
                hosts.append({"name": name, "runtime_id": "antigravity", "ident": ident,
                              "cwd": str(project), "pane_pid": pid})

            runtime = AntigravityRuntime()
            registry = RuntimeRegistry((runtime,))
            with (
                mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": str(root / "cache")}),
                mock.patch.object(parser, "_roots", return_value=[str(root)]),
                mock.patch.object(parser, "_read_all_summaries", return_value=summaries),
                mock.patch.object(liveness, "_list_antigravity_processes", return_value=({10001, 10002}, {})),
                mock.patch.object(liveness, "_list_lsof_lock_fds", return_value=descriptors),
                mock.patch.object(liveness, "list_managed_hosts", return_value=hosts),
                mock.patch.object(liveness, "_list_tmux_sessions", return_value=[
                    [host["name"], str(host["pane_pid"]), "agy"] for host in hosts
                ]),
                mock.patch.object(liveness, "_build_ppid_map", return_value={}),
                mock.patch.object(liveness, "is_alive", return_value=True),
                mock.patch("corral.scan_index._shared_index_enabled", return_value=False),
                mock.patch("corral.store.titles.load_cache", return_value={}),
            ):
                store = SessionStore(50, registry, AttentionStore(root / "attention.sqlite3"))
                for host in hosts:
                    store.register_hosted_session(
                        runtime_id="antigravity", keepalive_name=host["name"],
                        title="Antigravity New session", cwd=str(project), ident=host["ident"],
                    )
                keep_ids = {host["ident"] for host in hosts} | {"missing-old-id"}
                scanned = registry.scan_all(50, {"antigravity": keep_ids}, prefer_shared=False)
                self.assertEqual({s["id"] for s in scanned["antigravity"]}, set(paths))
                with mock.patch.object(store, "request_title_generation"):
                    store._merge_scanned(scanned)
                for cid, host in zip(paths, hosts, strict=True):
                    self.assertIsNone(store.find_session(f"antigravity:{host['ident']}"))
                    session = store.find_session(f"antigravity:{cid}")
                    self.assertIsNotNone(session)
                    self.assertEqual(session["keepalive_name"], host["name"])
                    self.assertEqual(session["path"], paths[cid])
                    self.assertLess(session["mtime"], now)
                    self.assertEqual(session["attention_kind"], "working")
                    self.assertNotIn("New session", store.display_titles[f"antigravity:{cid}"])
                    handoff = runtime.export_handoff(session, "Retention repair")
                    self.assertEqual(handoff.history_path, paths[cid])
                    self.assertIn("Repair retention task", handoff.conversation_digest)
