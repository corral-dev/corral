"""Tests for the read-only terminal text location kernel.

Behavioral coverage only: every test goes through find_prompt_candidates
with caller-supplied plain text rows. No terminal parsing, runtime, or
model interaction is involved.
"""

from __future__ import annotations

import time
import unittest

from corral.terminal_prompt_location import (
    PromptTextMatch,
    TerminalTextLine,
    find_prompt_candidates,
)


def rows(*texts: str, wrapped: tuple[int, ...] = ()) -> list[TerminalTextLine]:
    return [TerminalTextLine(text, wrapped=(i in wrapped)) for i, text in enumerate(texts)]


class SameRowMatchTests(unittest.TestCase):
    def test_unique_match_reports_code_point_coordinates(self):
        lines = rows("run the deploy plan now")
        self.assertEqual(
            find_prompt_candidates(lines, "deploy plan"),
            [PromptTextMatch(0, 8, 0, 19)],
        )

    def test_match_is_case_sensitive(self):
        lines = rows("Deploy plan")
        self.assertEqual(find_prompt_candidates(lines, "deploy plan"), [])

    def test_punctuation_is_significant(self):
        lines = rows("fix it! now")
        self.assertEqual(find_prompt_candidates(lines, "fix it now"), [])
        self.assertEqual(
            find_prompt_candidates(lines, "fix it! now"),
            [PromptTextMatch(0, 0, 0, 11)],
        )

    def test_all_duplicates_returned_in_physical_order(self):
        lines = rows("ship it", "review ship it twice", "ship it")
        self.assertEqual(
            find_prompt_candidates(lines, "ship it"),
            [
                PromptTextMatch(0, 0, 0, 7),
                PromptTextMatch(1, 7, 1, 14),
                PromptTextMatch(2, 0, 2, 7),
            ],
        )

    def test_overlapping_occurrences_all_returned(self):
        lines = rows("aaa")
        self.assertEqual(
            find_prompt_candidates(lines, "aa"),
            [PromptTextMatch(0, 0, 0, 2), PromptTextMatch(0, 1, 0, 3)],
        )

    def test_repeated_fragment_inside_one_row(self):
        lines = rows("ab ab ab")
        self.assertEqual(
            find_prompt_candidates(lines, "ab"),
            [
                PromptTextMatch(0, 0, 0, 2),
                PromptTextMatch(0, 3, 0, 5),
                PromptTextMatch(0, 6, 0, 8),
            ],
        )


class MultilineMatchTests(unittest.TestCase):
    def test_hard_break_matches_single_space_query(self):
        lines = rows("hello", "world")
        self.assertEqual(
            find_prompt_candidates(lines, "hello world"),
            [PromptTextMatch(0, 0, 1, 5)],
        )

    def test_hard_break_matches_multiline_query(self):
        lines = rows("hello", "world")
        self.assertEqual(
            find_prompt_candidates(lines, "hello\nworld"),
            [PromptTextMatch(0, 0, 1, 5)],
        )

    def test_soft_wrap_joins_without_separator(self):
        lines = rows("hello", "world", wrapped=(1,))
        self.assertEqual(
            find_prompt_candidates(lines, "hello world"),
            [],
        )
        self.assertEqual(
            find_prompt_candidates(lines, "helloworld"),
            [PromptTextMatch(0, 0, 1, 5)],
        )

    def test_soft_wrap_cjk_join_loses_no_characters(self):
        lines = rows("这是测", "试文本", wrapped=(1,))
        self.assertEqual(
            find_prompt_candidates(lines, "测试文本"),
            [PromptTextMatch(0, 2, 1, 3)],
        )

    def test_final_wide_character_at_wrap_is_preserved(self):
        lines = rows("报告结束语", "言在此处", wrapped=(1,))
        self.assertEqual(
            find_prompt_candidates(lines, "语言在此处"),
            [PromptTextMatch(0, 4, 1, 4)],
        )

    def test_supplied_spaces_at_wraps_are_retained(self):
        lines = rows("hello ", "world", wrapped=(1,))
        self.assertEqual(
            find_prompt_candidates(lines, "hello world"),
            [PromptTextMatch(0, 0, 1, 5)],
        )

    def test_first_line_wrapped_flag_is_ignored(self):
        plain = rows("hello world")
        flagged = [TerminalTextLine("hello world", wrapped=True)]
        self.assertEqual(
            find_prompt_candidates(flagged, "hello world"),
            find_prompt_candidates(plain, "hello world"),
        )

    def test_match_spanning_blank_line(self):
        lines = rows("ab", "", "cd")
        self.assertEqual(
            find_prompt_candidates(lines, "ab cd"),
            [PromptTextMatch(0, 0, 2, 2)],
        )


class WhitespaceNormalizationTests(unittest.TestCase):
    def test_source_tabs_and_repeated_spaces_collapse(self):
        lines = rows("hello   \tworld")
        self.assertEqual(
            find_prompt_candidates(lines, "hello world"),
            [PromptTextMatch(0, 0, 0, 14)],
        )

    def test_query_runs_collapse_to_single_space(self):
        lines = rows("hello world")
        self.assertEqual(
            find_prompt_candidates(lines, "hello   \t  world"),
            [PromptTextMatch(0, 0, 0, 11)],
        )

    def test_query_crlf_and_tabs_match_hard_break(self):
        lines = rows("alpha", "beta")
        self.assertEqual(
            find_prompt_candidates(lines, "alpha\r\n\tbeta"),
            [PromptTextMatch(0, 0, 1, 4)],
        )

    def test_query_is_trimmed(self):
        lines = rows("ship it")
        self.assertEqual(
            find_prompt_candidates(lines, "  ship it\t "),
            [PromptTextMatch(0, 0, 0, 7)],
        )

    def test_whitespace_run_around_hard_break_collapses(self):
        lines = rows("ab  ", "  cd")
        self.assertEqual(
            find_prompt_candidates(lines, "ab cd"),
            [PromptTextMatch(0, 0, 1, 4)],
        )

    def test_full_width_space_collapses(self):
        lines = rows("hello\u3000world")
        self.assertEqual(
            find_prompt_candidates(lines, "hello world"),
            [PromptTextMatch(0, 0, 0, 11)],
        )


class UnicodeCoordinateTests(unittest.TestCase):
    def test_emoji_counts_as_one_code_point(self):
        lines = rows("hi \U0001f600 bye")
        self.assertEqual(
            find_prompt_candidates(lines, "\U0001f600 bye"),
            [PromptTextMatch(0, 3, 0, 8)],
        )

    def test_cjk_coordinates_are_code_points(self):
        lines = rows("修复这个错误")
        self.assertEqual(
            find_prompt_candidates(lines, "这个错误"),
            [PromptTextMatch(0, 2, 0, 6)],
        )

    def test_combining_sequence_is_preserved(self):
        lines = rows("cafe\u0301 latte")
        self.assertEqual(
            find_prompt_candidates(lines, "cafe\u0301"),
            [PromptTextMatch(0, 0, 0, 5)],
        )

    def test_distinct_unicode_sequences_do_not_match(self):
        lines = rows("caf\u00e9")
        self.assertEqual(find_prompt_candidates(lines, "cafe\u0301"), [])
        self.assertEqual(
            find_prompt_candidates(lines, "caf\u00e9"),
            [PromptTextMatch(0, 0, 0, 4)],
        )

    def test_no_transliteration_or_case_folding(self):
        lines = rows("Stra\u00dfe Test")
        self.assertEqual(find_prompt_candidates(lines, "strasse test"), [])
        self.assertEqual(find_prompt_candidates(lines, "STRASSE TEST"), [])


class EmptyAndNoMatchTests(unittest.TestCase):
    def test_blank_query_returns_empty(self):
        self.assertEqual(find_prompt_candidates(rows("hello"), ""), [])

    def test_whitespace_only_query_returns_empty(self):
        self.assertEqual(find_prompt_candidates(rows("hello"), "  \t\r\n  "), [])

    def test_no_match_returns_empty(self):
        lines = rows("hello world")
        self.assertEqual(find_prompt_candidates(lines, "goodbye"), [])

    def test_empty_lines_returns_empty(self):
        self.assertEqual(find_prompt_candidates([], "hello"), [])

    def test_query_longer_than_text_returns_empty(self):
        self.assertEqual(find_prompt_candidates(rows("hi"), "hello world"), [])

    def test_non_string_prompt_rejected(self):
        with self.assertRaises(TypeError):
            find_prompt_candidates(rows("hello"), None)  # type: ignore[arg-type]


class ContractStabilityTests(unittest.TestCase):
    def test_source_lines_unchanged(self):
        lines = rows("hello  ", "  world", wrapped=(1,))
        snapshot = [(line.text, line.wrapped) for line in lines]
        find_prompt_candidates(lines, "hello world")
        self.assertEqual([(line.text, line.wrapped) for line in lines], snapshot)

    def test_output_is_deterministic(self):
        lines = rows("ship it", "ship it again", "ship it")
        first = find_prompt_candidates(lines, "ship it")
        second = find_prompt_candidates(lines, "ship it")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 3)

    def test_invalid_match_coordinates_rejected(self):
        with self.assertRaises(ValueError):
            PromptTextMatch(1, 0, 0, 5)
        with self.assertRaises(ValueError):
            PromptTextMatch(0, -1, 0, 2)
        with self.assertRaises(TypeError):
            PromptTextMatch(0, "0", 0, 2)  # type: ignore[arg-type]


class LongHistoryTests(unittest.TestCase):
    def _build_history(self, count: int) -> list[TerminalTextLine]:
        lines: list[TerminalTextLine] = []
        for i in range(count):
            if i % 3 == 0:
                lines.append(TerminalTextLine(f"[{i}] 部署状态检查通过  \u2713 ok"))
            elif i % 3 == 1:
                # Soft-wrapped continuation pair with CJK and emoji.
                lines.append(TerminalTextLine(f"第{i}轮 agent 输出片段 \U0001f680"))
                lines.append(TerminalTextLine("续行内容 done", wrapped=True))
            else:
                lines.append(TerminalTextLine(f"log line {i}\tresult=ok"))
        return lines

    def test_long_history_finds_late_prompt_quickly(self):
        lines = self._build_history(6000)
        target_row = len(lines) - 1
        lines.append(TerminalTextLine("please refactor the retry helper"))
        started = time.perf_counter()
        matches = find_prompt_candidates(lines, "refactor the retry helper")
        elapsed = time.perf_counter() - started
        self.assertEqual(
            matches,
            [PromptTextMatch(target_row + 1, 7, target_row + 1, 32)],
        )
        self.assertLess(elapsed, 5.0, f"long-history scan took {elapsed:.2f}s")

    def test_long_history_reports_every_duplicate(self):
        lines = self._build_history(3000)
        lines.append(TerminalTextLine("deploy now"))
        lines.append(TerminalTextLine("deploy now"))
        matches = find_prompt_candidates(lines, "deploy now")
        self.assertEqual(len(matches), 2)
        self.assertLess(matches[0].start_line, matches[1].start_line)

    def test_near_miss_query_scans_without_quadratic_blowup(self):
        lines = [TerminalTextLine("a" * 2000) for _ in range(100)]
        started = time.perf_counter()
        matches = find_prompt_candidates(lines, "a" * 60 + "b")
        elapsed = time.perf_counter() - started
        self.assertEqual(matches, [])
        self.assertLess(elapsed, 5.0, f"near-miss scan took {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
