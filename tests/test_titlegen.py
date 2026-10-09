from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral import titlegen


class _Response:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int = -1) -> bytes:
        return self.body


class GatewayTitleGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        titlegen.clear_model_cache()
        self.config = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(
            {
                "base_url": "http://gateway.test/v1",
                "api_key": "vk-test",
                "model": "budget-chat",
            },
            self.config,
        )
        self.config.close()
        self.addCleanup(os.unlink, self.config.name)
        self.addCleanup(titlegen.clear_model_cache)

    def _env(self, **extra):
        env = {titlegen.ENV_CONFIG: self.config.name, **extra}
        return mock.patch.dict(os.environ, env, clear=True)

    def test_uses_gateway_only_and_returns_content(self) -> None:
        seen = {}

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["headers"] = dict(request.header_items())
            seen["timeout"] = timeout
            seen["payload"] = json.loads(request.data)
            return _Response(b'{"choices":[{"message":{"content":"Title"}}]}')

        with self._env(), mock.patch.object(titlegen.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = titlegen.GatewayTitleGenerator().generate("prompt", 7)
        self.assertEqual(result, "Title")
        self.assertEqual(seen["url"], "http://gateway.test/v1/chat/completions")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer vk-test")
        self.assertEqual(seen["payload"]["model"], "budget-chat")
        self.assertEqual(seen["payload"]["temperature"], 0)
        self.assertEqual(seen["payload"]["max_tokens"], 2048)
        self.assertEqual(seen["timeout"], 7)

    def test_missing_key_is_unavailable_and_makes_no_request(self) -> None:
        Path(self.config.name).write_text(
            json.dumps({"base_url": "http://gateway.test/v1", "model": "budget-chat"}),
            encoding="utf-8",
        )
        with self._env(), mock.patch.object(titlegen.urllib.request, "urlopen") as request:
            self.assertEqual(titlegen.available_generators(), ())
            self.assertFalse(titlegen.GatewayTitleGenerator().is_available())
            self.assertIsNone(titlegen.GatewayTitleGenerator().generate("prompt", 1))
            request.assert_not_called()

    def test_key_without_model_is_available_and_resolves_live_catalog(self) -> None:
        Path(self.config.name).write_text(
            json.dumps({"base_url": "http://gateway.test/v1", "api_key": "vk-test"}),
            encoding="utf-8",
        )
        catalog = {
            "data": [
                {"id": "default-chat"},
                {"id": "budget-chat"},
                {"id": "gemini-chat"},
            ]
        }
        seen = {}

        def fake_urlopen(request, timeout=None):
            url = request.full_url
            if url.endswith("/models"):
                return _Response(json.dumps(catalog).encode("utf-8"))
            seen["url"] = url
            seen["payload"] = json.loads(request.data)
            return _Response(b'{"choices":[{"message":{"content":"From catalog"}}]}')

        with self._env(), mock.patch.object(titlegen.urllib.request, "urlopen", side_effect=fake_urlopen):
            self.assertTrue(titlegen.GatewayTitleGenerator().is_available())
            self.assertEqual(titlegen.title_model(), "budget-chat")
            self.assertEqual(
                titlegen.GatewayTitleGenerator().generate("prompt", 3),
                "From catalog",
            )
        self.assertEqual(seen["payload"]["model"], "budget-chat")

    def test_live_catalog_falls_back_to_first_alias_when_budget_missing(self) -> None:
        Path(self.config.name).write_text(
            json.dumps({"base_url": "http://gateway.test/v1", "api_key": "vk-test"}),
            encoding="utf-8",
        )
        catalog = {"data": [{"id": "default-chat"}, {"id": "gemini-chat"}]}

        def fake_urlopen(request, timeout=None):
            return _Response(json.dumps(catalog).encode("utf-8"))

        with self._env(), mock.patch.object(titlegen.urllib.request, "urlopen", side_effect=fake_urlopen):
            self.assertEqual(titlegen.title_model(), "default-chat")

    def test_list_content_blocks_are_joined(self) -> None:
        body = {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": '{"a": "Title A", '},
                            {"type": "text", "text": '"b": "Title B"}'},
                        ]
                    }
                }
            ]
        }

        def fake_urlopen(request, timeout):
            return _Response(json.dumps(body).encode("utf-8"))

        with self._env(), mock.patch.object(titlegen.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = titlegen.GatewayTitleGenerator().generate("prompt", 7)
        self.assertEqual(result, '{"a": "Title A", "b": "Title B"}')

    def test_list_content_ignores_non_text_blocks(self) -> None:
        body = {
            "choices": [
                {"message": {"content": [{"type": "image_url", "image_url": {}}, "tail"]}}
            ]
        }

        def fake_urlopen(request, timeout):
            return _Response(json.dumps(body).encode("utf-8"))

        with self._env(), mock.patch.object(titlegen.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = titlegen.GatewayTitleGenerator().generate("prompt", 7)
        self.assertEqual(result, "tail")

    def test_empty_list_content_is_a_failure(self) -> None:
        body = {"choices": [{"message": {"content": []}}]}

        def fake_urlopen(request, timeout):
            return _Response(json.dumps(body).encode("utf-8"))

        with self._env(), mock.patch.object(titlegen.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = titlegen.GatewayTitleGenerator().generate("prompt", 7)
        self.assertIsNone(result)

    def test_http_failure_and_malformed_response_are_failures(self) -> None:
        with self._env(), mock.patch.object(titlegen.urllib.request, "urlopen", side_effect=OSError):
            self.assertIsNone(titlegen.GatewayTitleGenerator().generate("prompt", 1))
        with self._env(), mock.patch.object(
            titlegen.urllib.request, "urlopen", return_value=_Response(b'{"choices": []}')
        ):
            self.assertIsNone(titlegen.GatewayTitleGenerator().generate("prompt", 1))

    def test_environment_overrides_durable_config(self) -> None:
        with self._env(), mock.patch.dict(os.environ, {titlegen.ENV_MODEL: "default-chat"}, clear=False):
            self.assertEqual(titlegen.title_model(), "default-chat")

    def test_missing_url_with_key_makes_no_request(self) -> None:
        for model in (None, "budget-chat"):
            config = {"api_key": "vk-test"}
            if model:
                config["model"] = model
            Path(self.config.name).write_text(json.dumps(config), encoding="utf-8")
            with self.subTest(model=model), self._env(), mock.patch.object(
                titlegen.urllib.request, "urlopen"
            ) as request:
                self.assertEqual(titlegen.available_generators(), ())
                self.assertFalse(titlegen.GatewayTitleGenerator().is_available())
                self.assertIsNone(titlegen.GatewayTitleGenerator().generate("prompt", 1))
                request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
