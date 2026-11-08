"""Path mentions under a TUI click (corral.path_links); temporary directories only."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral import path_links


class PathAtTests(unittest.TestCase):
    def test_finds_the_mention_under_the_cell(self):
        line = "see src/app.py:12, then ~/notes/todo.md."
        self.assertEqual(path_links.path_at(line, 6), "src/app.py:12")
        self.assertEqual(path_links.path_at(line, line.index("todo")), "~/notes/todo.md")
        self.assertIsNone(path_links.path_at(line, 0))

    def test_wide_characters_map_cells_to_characters(self):
        line = "修改了 /tmp/a.txt"
        # "修改了 " is 7 cells wide, so cell 7 is the slash.
        self.assertEqual(path_links.path_at(line, 7), "/tmp/a.txt")
        self.assertIsNone(path_links.path_at(line, 3))

    def test_prose_and_urls_are_not_paths(self):
        line = "and/or e.g. https://example.com/a/b"
        self.assertEqual(path_links.path_at(line, 1), "and/or")  # a candidate; resolve() rejects it
        self.assertIsNone(path_links.path_at(line, line.index("example")))


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "docs").mkdir()
        (self.root / "docs" / "guide.md").write_text("x")

    def test_relative_absolute_and_line_suffixes(self):
        cwd = str(self.root)
        guide = str(self.root / "docs" / "guide.md")
        self.assertEqual(path_links.resolve("docs/guide.md:3", cwd), guide)
        self.assertEqual(path_links.resolve(f"{guide}#L4-L9", None), guide)
        self.assertEqual(path_links.resolve("docs", cwd), str(self.root / "docs"))

    def test_deleted_file_opens_its_folder_but_prose_does_not_resolve(self):
        cwd = str(self.root)
        self.assertEqual(path_links.resolve("docs/missing.txt", cwd), str(self.root / "docs"))
        self.assertIsNone(path_links.resolve("and/or", cwd))
        self.assertIsNone(path_links.resolve("missing.txt", cwd))
        self.assertIsNone(path_links.resolve("docs/guide.md", None))

    def test_home_paths_expand(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            self.assertEqual(path_links.resolve("~/docs/guide.md", None), str(self.root / "docs" / "guide.md"))


class RevealTests(unittest.TestCase):
    def test_file_is_selected_and_folder_is_opened_on_macos(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(path_links.sys, "platform", "darwin"), \
                mock.patch.object(path_links.subprocess, "Popen") as popen:
            file = Path(tmp) / "a.txt"
            file.write_text("x")
            self.assertTrue(path_links.reveal(str(file)))
            self.assertTrue(path_links.reveal(tmp))
        self.assertEqual(popen.call_args_list[0][0][0], ["open", "-R", str(file)])
        self.assertEqual(popen.call_args_list[1][0][0], ["open", tmp])
