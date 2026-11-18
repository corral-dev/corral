"""corral.remote.media + SessionHub.image_preview: bounded previews of cited images."""

from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral import split_layout
from corral.remote import media, protocol
from corral.remote.sessions import ActionError, SessionHub

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow is part of the [remote] extra
    Image = None


def _png(width: int, height: int, *, alpha: bool = False) -> bytes:
    assert Image is not None
    mode = "RGBA" if alpha else "RGB"
    color = (200, 40, 40, 128) if alpha else (200, 40, 40)
    buffer = io.BytesIO()
    Image.new(mode, (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


class MediaEncodingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_clamps_and_budget(self) -> None:
        self.assertEqual(media.clamp_px(0), media.DEFAULT_PX)
        self.assertEqual(media.clamp_px(10), media.MIN_PX)
        self.assertEqual(media.clamp_px(99_999), media.MAX_PX)
        self.assertEqual(media.clamp_quality(5), media.MIN_QUALITY)
        self.assertLess(media.budget_for(640), media.budget_for(1600))

    def test_resolve_local_relative_home_and_file_url(self) -> None:
        target = self.root / "shots" / "a.png"
        target.parent.mkdir()
        target.write_bytes(b"x")
        self.assertEqual(media.resolve_local("shots/a.png", str(self.root)), target.resolve())
        self.assertEqual(media.resolve_local(f"file://{target}", None), target.resolve())
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            self.assertEqual(media.resolve_local("~/shots/a.png", None), target.resolve())
        with self.assertRaises(media.MediaError) as missing:
            media.resolve_local("shots/none.png", str(self.root))
        self.assertEqual(missing.exception.code, "not_found")
        (self.root / "notes.txt").write_text("hi")
        with self.assertRaises(media.MediaError) as wrong:
            media.resolve_local("notes.txt", str(self.root))
        self.assertEqual(wrong.exception.code, "unavailable")

    def test_non_image_bytes_are_unavailable(self) -> None:
        fake = self.root / "fake.png"
        fake.write_bytes(b"not an image at all")
        service = media.PreviewService(encoders=())
        with self.assertRaises(media.MediaError) as caught:
            service.preview(str(fake), cwd=None, max_px=640, quality=60)
        self.assertEqual(caught.exception.code, "unavailable")

    def test_small_original_passes_through_without_encoders(self) -> None:
        raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        service = media.PreviewService(encoders=())
        preview = service.encode(raw, max_px=640, quality=60)
        self.assertEqual(preview.mime, "image/png")
        self.assertEqual(preview.data, raw)

    def test_large_original_without_encoders_is_unavailable(self) -> None:
        raw = b"\xff\xd8\xff" + b"\x00" * (media.budget_for(640) + 1)
        service = media.PreviewService(encoders=())
        with self.assertRaises(media.MediaError):
            service.encode(raw, max_px=640, quality=60)

    @unittest.skipIf(Image is None, "Pillow not installed")
    def test_pillow_downscales_to_long_edge_and_jpeg(self) -> None:
        source = self.root / "big.png"
        source.write_bytes(_png(3000, 1500))
        preview = media.PreviewService().preview(str(source), cwd=None, max_px=640, quality=60)
        self.assertEqual(preview.mime, "image/jpeg")
        self.assertEqual((preview.width, preview.height), (640, 320))
        self.assertEqual((preview.source_width, preview.source_height), (3000, 1500))
        self.assertLessEqual(len(preview.data), media.budget_for(640))
        wire = preview.to_wire()
        self.assertEqual(base64.b64decode(wire["data"]), preview.data)

    @unittest.skipIf(Image is None, "Pillow not installed")
    def test_pillow_keeps_alpha_as_png_when_it_fits(self) -> None:
        source = self.root / "alpha.png"
        source.write_bytes(_png(200, 100, alpha=True))
        preview = media.PreviewService().preview(str(source), cwd=None, max_px=640, quality=60)
        self.assertEqual(preview.mime, "image/png")
        self.assertEqual((preview.width, preview.height), (200, 100))

    def test_url_fetch_goes_through_fetcher_and_caches(self) -> None:
        calls: list[str] = []
        raw = b"GIF89a" + b"\x00" * 32

        def fetch(url: str) -> bytes:
            calls.append(url)
            return raw

        service = media.PreviewService(fetch=fetch, encoders=())
        first = service.preview("https://example.com/a.gif", cwd=None, max_px=640, quality=60)
        second = service.preview("https://example.com/a.gif", cwd=None, max_px=640, quality=60)
        self.assertEqual(first, second)
        self.assertEqual(calls, ["https://example.com/a.gif"])


class HubImagePreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": self._tmp.name}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)
        self.hub = SessionHub(scan_limit=10)
        self.hub.layout_db = split_layout.SidebarLayoutDB()
        self.addCleanup(self.hub.stop)
        (self.root / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        (self.root / "secret.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x01" * 64)
        history = self.root / "claude.jsonl"
        history.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Saved ![shot](shot.png) for you"}],
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        self.session = {
            "source": "claude",
            "id": "img",
            "short_id": "img",
            "cwd": str(self.root),
            "cwd_display": str(self.root),
            "mtime": 1_700_000_000.0,
            "display_time": "",
            "size_kb": 1.0,
            "status_tag": "ended",
            "live": False,
            "keepalive_name": None,
            "fallback_title": "img",
            "attention_kind": "none",
            "last_user_msg": "",
            "last_agent_msg": "",
            "path": str(history),
        }
        self.hub._media = media.PreviewService(encoders=())

    def _seq(self) -> int:
        with mock.patch.object(self.hub, "require_session", return_value=self.session):
            page = self.hub.message_page("claude:img")
        return page["messages"][0]["seq"]

    def test_referenced_image_previews(self) -> None:
        seq = self._seq()
        with mock.patch.object(self.hub, "require_session", return_value=self.session):
            payload = self.hub.image_preview(
                "claude:img", seq=seq, ref="shot.png", max_px=640, quality=60
            )
        self.assertEqual(payload["mime"], "image/png")
        decoded = protocol.loads(protocol.dumps(protocol.response(1, payload)))
        self.assertEqual(decoded["d"]["bytes"], payload["bytes"])

    def test_unreferenced_path_is_refused(self) -> None:
        seq = self._seq()
        with mock.patch.object(self.hub, "require_session", return_value=self.session):
            with self.assertRaises(ActionError) as caught:
                self.hub.image_preview(
                    "claude:img", seq=seq, ref="secret.png", max_px=640, quality=60
                )
        self.assertEqual(caught.exception.code, "not_found")

    def test_missing_message_is_not_found(self) -> None:
        with mock.patch.object(self.hub, "require_session", return_value=self.session):
            with self.assertRaises(ActionError) as caught:
                self.hub.image_preview(
                    "claude:img", seq=999, ref="shot.png", max_px=640, quality=60
                )
        self.assertEqual(caught.exception.code, "not_found")

    def test_media_image_is_readonly_and_advertised(self) -> None:
        from corral.remote import service

        self.assertIn(protocol.M_MEDIA_IMAGE, service._READONLY_METHODS)
        self.assertIn(protocol.M_MEDIA_IMAGE, service._HANDLERS)


class HubFileDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hub = SessionHub()
        self.markdown_file = self.root / "CORE PROFILE.md"
        self.markdown_file.write_text("# Profile\n\nHello **world**!\n", encoding="utf-8")
        self.large_file = self.root / "chunked.bin"
        self.large_file.write_bytes(b"A" * 128 + b"B" * 128)
        self.subdir = self.root / "folder"
        self.subdir.mkdir()

        history = self.root / "session.jsonl"
        file_url = self.markdown_file.as_uri() + "#L10-L20"
        text = (
            f"Read [CORE PROFILE.md]({file_url}), "
            f"[chunked.bin](chunked.bin), "
            f"[folder]({self.subdir.as_uri()}), "
            f"[missing](missing.md)."
        )
        history.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "msg-file-1",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        self.session = {
            "source": "claude",
            "id": "files",
            "short_id": "files",
            "cwd": str(self.root),
            "cwd_display": str(self.root),
            "mtime": 1_700_000_000.0,
            "display_time": "",
            "size_kb": 1.0,
            "status_tag": "ended",
            "live": False,
            "keepalive_name": None,
            "fallback_title": "files",
            "attention_kind": "none",
            "last_user_msg": "",
            "last_agent_msg": "",
            "path": str(history),
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _seq(self) -> int:
        with mock.patch.object(self.hub, "require_session", return_value=self.session):
            page = self.hub.message_page("claude:files")
        return page["messages"][0]["seq"]

    def test_file_url_with_spaces_and_line_fragment_downloads(self) -> None:
        import base64

        seq = self._seq()
        with mock.patch.object(self.hub, "require_session", return_value=self.session):
            payload = self.hub.file_slice(
                "claude:files",
                seq=seq,
                ref=self.markdown_file.as_uri() + "#L10-L20",
            )
        self.assertEqual(payload["name"], "CORE PROFILE.md")
        self.assertEqual(payload["mime"], "text/markdown")
        self.assertTrue(payload["eof"])
        self.assertEqual(
            base64.b64decode(payload["data"]).decode("utf-8"),
            "# Profile\n\nHello **world**!\n",
        )

    def test_chunked_file_download_reassembles_and_verifies_sha(self) -> None:
        import base64
        import hashlib

        seq = self._seq()
        with mock.patch.object(self.hub, "require_session", return_value=self.session):
            first = self.hub.file_slice(
                "claude:files", seq=seq, ref="chunked.bin", offset=0, max_bytes=128
            )
            second = self.hub.file_slice(
                "claude:files", seq=seq, ref="chunked.bin", offset=128, max_bytes=128
            )
        self.assertFalse(first["eof"])
        self.assertTrue(second["eof"])
        combined = base64.b64decode(first["data"]) + base64.b64decode(second["data"])
        self.assertEqual(combined, b"A" * 128 + b"B" * 128)
        self.assertEqual(first["sha256"], hashlib.sha256(combined).hexdigest())

    def test_directory_and_oversized_and_unreferenced_files_are_rejected(self) -> None:
        seq = self._seq()
        with mock.patch.object(self.hub, "require_session", return_value=self.session):
            with self.assertRaises(ActionError) as dir_err:
                self.hub.file_slice("claude:files", seq=seq, ref=self.subdir.as_uri())
            self.assertEqual(dir_err.exception.code, "unavailable")

            with self.assertRaises(ActionError) as missing_err:
                self.hub.file_slice("claude:files", seq=seq, ref="missing.md")
            self.assertEqual(missing_err.exception.code, "not_found")

            secret = self.root / "secret.txt"
            secret.write_text("top-secret", encoding="utf-8")
            with self.assertRaises(ActionError) as unref_err:
                self.hub.file_slice("claude:files", seq=seq, ref=str(secret))
            self.assertEqual(unref_err.exception.code, "not_found")

    def test_oversized_file_is_unavailable(self) -> None:
        seq = self._seq()
        with mock.patch.object(self.hub, "require_session", return_value=self.session):
            with mock.patch.object(media, "MAX_FILE_BYTES", 10):
                with self.assertRaises(ActionError) as caught:
                    self.hub.file_slice("claude:files", seq=seq, ref="chunked.bin")
        self.assertEqual(caught.exception.code, "unavailable")

    def test_media_file_is_readonly_data_plane_and_advertised(self) -> None:
        from corral.remote import service

        self.assertIn(protocol.M_MEDIA_FILE, service._READONLY_METHODS)
        self.assertIn(protocol.M_MEDIA_FILE, service._DATA_PAYLOAD_METHODS)
        self.assertIn(protocol.M_MEDIA_FILE, service._HANDLERS)


if __name__ == "__main__":
    unittest.main()
