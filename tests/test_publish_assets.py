"""Publication must preserve existing packages, including a concurrent winner."""

import importlib.util
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "publish_assets.py"
SPEC = importlib.util.spec_from_file_location("publish_assets", SCRIPT)
assert SPEC and SPEC.loader
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


class PublishAssetsTest(unittest.TestCase):
    def exercise(self, initial, upload_error=False):
        payload = b"published bytes differ from local build"
        digest = publisher.hashlib.sha256(payload).hexdigest()
        remote = {"corral.whl": {"digest": f"sha256:{digest}"}}
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            if "download" in argv:
                pathlib.Path(argv[argv.index("--dir") + 1], "corral.whl").write_bytes(payload)
            if "upload" in argv and argv[-1] == "--clobber":
                self.assertTrue(argv[4].endswith("SHA256SUMS"))
                self.assertEqual(pathlib.Path(argv[4]).read_text(), f"{digest}  corral.whl\n")
            if "upload" in argv and "--clobber" not in argv and upload_error:
                return subprocess.CompletedProcess(argv, 1, "", "already exists")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            dist = pathlib.Path(tmp)
            (dist / "corral.whl").write_bytes(b"new build")
            with patch.object(publisher, "release_assets", side_effect=[initial, remote, remote]), patch.object(
                publisher.subprocess, "run", side_effect=run
            ):
                publisher.publish("owner/repo", "v1", dist)
        package_uploads = [a for a in calls if "upload" in a and "--clobber" not in a]
        return package_uploads

    def test_existing_package_is_preserved_and_sums_use_remote_bytes(self):
        self.assertEqual(self.exercise({"corral.whl": {}}), [])

    def test_concurrent_upload_winner_is_preserved(self):
        self.assertEqual(len(self.exercise({}, upload_error=True)), 1)

    def test_failed_upload_without_remote_package_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = pathlib.Path(tmp)
            (dist / "corral.whl").write_bytes(b"build")
            with patch.object(publisher, "release_assets", return_value={}), patch.object(
                publisher.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "offline")
            ), self.assertRaisesRegex(RuntimeError, "Upload failed"):
                publisher.publish("owner/repo", "v1", dist)


class DownloadRetryTest(unittest.TestCase):
    def test_timeout_is_retried_then_reported(self):
        results = iter([1, 0])
        with patch.object(publisher.time, "sleep"), patch.object(
            publisher.subprocess, "run",
            side_effect=lambda argv: subprocess.CompletedProcess(argv, next(results)),
        ) as run:
            publisher.download("owner/repo", "v1", "corral.whl", pathlib.Path("/tmp"))
        self.assertEqual(run.call_count, 2)
        with patch.object(publisher.time, "sleep"), patch.object(
            publisher.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)
        ), self.assertRaisesRegex(RuntimeError, "Download failed 3 times"):
            publisher.download("owner/repo", "v1", "corral.whl", pathlib.Path("/tmp"))
