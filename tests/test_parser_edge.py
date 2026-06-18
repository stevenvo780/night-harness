#!/usr/bin/env python3
"""Edge-case tests for the bug parser (_parse_bug_line) in harness.py.

Covers malformed JSON, weird fallback formats, case-sensitive NO_BUGS,
comment-only lines, truncation limits, invalid extensions and legacy
multi-`::` separators.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402


class TestParseBugLineEdgeCases(unittest.TestCase):
    """Parser edge cases that are not covered by test_parser.py."""

    def test_json_truncated_snippet_empty_why_present(self):
        """JSON with snippet='' but why present must still return a record."""
        r = harness._parse_bug_line(
            '{"file":"lib/a.ts","line":7,"snippet":"","why":"possible n+1"}'
        )
        self.assertIsNotNone(r)
        self.assertEqual(r['file'], 'lib/a.ts')
        self.assertEqual(r['line'], 7)
        self.assertEqual(r['snippet'], '')
        self.assertEqual(r['why'], 'possible n+1')
        self.assertEqual(r['repro'], '')
        self.assertEqual(r['severity'], 'medium')

    def test_json_line_zero(self):
        """line=0 is a valid integer and should be accepted."""
        r = harness._parse_bug_line(
            '{"file":"lib/a.ts","line":0,"why":"edge"}'
        )
        self.assertIsNotNone(r)
        self.assertEqual(r['line'], 0)

    def test_json_file_with_slashes(self):
        """file with deep slashes and panel groups is preserved."""
        r = harness._parse_bug_line(
            '{"file":"app/(panel)/foo/bar/baz/page.tsx","line":42,"why":"x"}'
        )
        self.assertIsNotNone(r)
        self.assertEqual(r['file'], 'app/(panel)/foo/bar/baz/page.tsx')

    def test_fallback_path_with_quotes_rejected(self):
        """Path containing quotes is too malformed and must be rejected."""
        r = harness._parse_bug_line('"app/x.ts":5 - bug here')
        self.assertIsNone(r)

    def test_fallback_path_with_spaces_rejected(self):
        """Path containing spaces does not match the strict PATH regex."""
        r = harness._parse_bug_line('app/my file.ts:10 - bug')
        self.assertIsNone(r)

    def test_fallback_path_with_special_chars_rejected(self):
        """Paths with unallowed specials ($, @) are rejected."""
        r = harness._parse_bug_line('app/$file.ts:10 - bug')
        self.assertIsNone(r)
        r = harness._parse_bug_line('app/file@2x.ts:10 - bug')
        self.assertIsNone(r)

    def test_no_bugs_exact_is_rejected(self):
        """Exact uppercase NO_BUGS short-circuits to None."""
        self.assertIsNone(harness._parse_bug_line('NO_BUGS'))
        self.assertIsNone(harness._parse_bug_line('NO_BUGS '))
        self.assertIsNone(harness._parse_bug_line('NO_BUGS\n'))

    def test_no_bugs_lowercase_is_not_special(self):
        """no_bugs (lowercase) is NOT treated as sentinel and parses as JSON/fallback."""
        self.assertIsNone(harness._parse_bug_line('no_bugs'))
        self.assertIsNone(harness._parse_bug_line('no_bugs extra'))

    def test_comment_only_lines(self):
        """Lines that are only comments are rejected."""
        for line in ('#', '# comment', '//', '// comment', '/*', '/**'):
            self.assertIsNone(
                harness._parse_bug_line(line),
                f'should reject comment-only line: {line!r}'
            )

    def test_snippet_truncated_to_200(self):
        """Very long snippet is truncated to 200 characters."""
        long_snippet = 'x' * 250
        r = harness._parse_bug_line(
            f'{{"file":"a.ts","line":1,"snippet":"{long_snippet}","why":"y"}}'
        )
        self.assertIsNotNone(r)
        self.assertEqual(len(r['snippet']), 200)
        self.assertEqual(r['snippet'], 'x' * 200)

    def test_why_truncated_to_500(self):
        """Very long why is truncated to 500 characters."""
        long_why = 'y' * 600
        r = harness._parse_bug_line(
            f'{{"file":"a.ts","line":1,"snippet":"s","why":"{long_why}"}}'
        )
        self.assertIsNotNone(r)
        self.assertEqual(len(r['why']), 500)
        self.assertEqual(r['why'], 'y' * 500)

    def test_repro_truncated_to_300(self):
        """Very long repro is truncated to 300 characters."""
        long_repro = 'z' * 400
        r = harness._parse_bug_line(
            f'{{"file":"a.ts","line":1,"snippet":"s","why":"w","repro":"{long_repro}"}}'
        )
        self.assertIsNotNone(r)
        self.assertEqual(len(r['repro']), 300)
        self.assertEqual(r['repro'], 'z' * 300)

    def test_fallback_snippet_truncated_to_200(self):
        """Legacy fallback also truncates snippet to 200 chars."""
        snippet = 'x' * 250
        line = f'lib/a.ts:1 {snippet}::reason'
        r = harness._parse_bug_line(line)
        self.assertIsNotNone(r)
        self.assertEqual(len(r['snippet']), 200)

    def test_fallback_why_truncated_to_500(self):
        """Legacy fallback also truncates why to 500 chars."""
        why = 'y' * 600
        line = f'lib/a.ts:1 snippet::{why}'
        r = harness._parse_bug_line(line)
        self.assertIsNotNone(r)
        self.assertEqual(len(r['why']), 500)

    def test_file_without_valid_extension_rejected(self):
        """Paths without a 1-5 char lowercase extension are rejected."""
        self.assertIsNone(harness._parse_bug_line('Makefile:10 - bug'))
        self.assertIsNone(harness._parse_bug_line('Dockerfile:5 - bug'))
        self.assertIsNone(harness._parse_bug_line('README:1 - bug'))

    def test_file_with_one_char_extension_accepted(self):
        """Single-character lowercase extension is accepted."""
        r = harness._parse_bug_line('lib/x.c:10 - bug')
        self.assertIsNotNone(r)
        self.assertEqual(r['file'], 'lib/x.c')

    def test_file_with_five_char_extension_accepted(self):
        """Five-character lowercase extension is accepted."""
        r = harness._parse_bug_line('lib/x.story:10 - bug')
        self.assertIsNotNone(r)
        self.assertEqual(r['file'], 'lib/x.story')

    def test_file_with_six_char_extension_rejected(self):
        """Six-character extension exceeds the regex limit and is rejected."""
        self.assertIsNone(harness._parse_bug_line('lib/x.foobar:10 - bug'))

    def test_file_uppercase_extension_rejected(self):
        """Uppercase extension does not match [a-z]{1,5}."""
        self.assertIsNone(harness._parse_bug_line('lib/x.TS:10 - bug'))

    def test_legacy_three_colon_separator_take_first_and_last(self):
        """file:line::snippet::why::reason must take snippet and reason, not duplicate."""
        r = harness._parse_bug_line(
            'app/page.tsx:42::const x=1::middle noise::real reason text'
        )
        self.assertIsNotNone(r)
        self.assertEqual(r['snippet'], 'const x=1')
        self.assertEqual(r['why'], 'real reason text')
        self.assertNotEqual(r['snippet'], r['why'])
        self.assertNotIn('::', r['snippet'])
        self.assertNotIn('::', r['why'])

    def test_legacy_many_colon_separator_take_first_and_last(self):
        """More than 3 separators still takes first and last parts."""
        r = harness._parse_bug_line(
            'app/page.tsx:1::a::b::c::d::e'
        )
        self.assertIsNotNone(r)
        self.assertEqual(r['snippet'], 'a')
        self.assertEqual(r['why'], 'e')


if __name__ == '__main__':
    unittest.main(verbosity=2)
