#!/usr/bin/env python3
"""Edge-case tests for the fix gate: route mapping and patch detection."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402


class TestRouteForFileEdgeCases(unittest.TestCase):
    """_route_for_file edge cases around (panel) groups and non-routable paths."""

    def test_panel_single_level(self):
        """app/(panel)/x maps to /x."""
        self.assertEqual(harness._route_for_file('app/(panel)/x/page.tsx'), '/x')

    def test_panel_nested_stops_at_first_level(self):
        """app/(panel)/x/y/z.tsx must return /x, not deeper segments."""
        self.assertEqual(harness._route_for_file('app/(panel)/x/y/z.tsx'), '/x')
        self.assertEqual(
            harness._route_for_file('app/(panel)/dashboard/reports/yearly.tsx'),
            '/dashboard'
        )

    def test_plain_app_route(self):
        """app/login/page.tsx returns /login (no (panel) group)."""
        self.assertEqual(harness._route_for_file('app/login/page.tsx'), '/login')

    def test_components_not_routeable(self):
        """components/X.tsx is not an app route."""
        self.assertIsNone(harness._route_for_file('components/X.tsx'))

    def test_empty_and_none_paths(self):
        """Empty string and None return None."""
        self.assertIsNone(harness._route_for_file(''))
        self.assertIsNone(harness._route_for_file(None))


class TestLooksLikePatchEdgeCases(unittest.TestCase):
    """_looks_like_patch edge cases around multi-file diffs and false positives."""

    def test_multifile_diff_three_files(self):
        """A diff touching 3+ files is recognized as a patch."""
        diff = (
            '--- a/lib/x.ts\n+++ b/lib/x.ts\n@@ -1 +1 @@\n-old x\n+new x\n'
            '--- a/lib/y.ts\n+++ b/lib/y.ts\n@@ -1 +1 @@\n-old y\n+new y\n'
            '--- a/lib/z.ts\n+++ b/lib/z.ts\n@@ -1 +1 @@\n-old z\n+new z'
        )
        self.assertTrue(harness._looks_like_patch(diff))

    def test_line_with_at_at_but_no_diff_headers_is_not_patch(self):
        """A bare '@@' line without '--- ' is not a patch."""
        text = 'some text\n@@ -1,2 +1,2 @@\nmore text'
        self.assertFalse(harness._looks_like_patch(text))

    def test_full_file_with_literal_dashes_not_patch(self):
        """Source file containing '---' literally is not a patch unless it starts with it."""
        text = 'const x = 1;\n// --- comment\nexport default x;'
        self.assertFalse(harness._looks_like_patch(text))


class TestPatchFilesEdgeCases(unittest.TestCase):
    """_patch_files extraction edge cases."""

    def test_patch_files_multifile(self):
        """Extract all files from a multi-file unified diff."""
        diff = (
            '--- a/src/x.ts\n+++ b/src/x.ts\n@@ -1 +1 @@\n-a\n+b\n'
            '--- a/src/y.ts\n+++ b/src/y.ts\n@@ -1 +1 @@\n-c\n+d\n'
            '--- a/src/z.ts\n+++ b/src/z.ts\n@@ -1 +1 @@\n-e\n+f'
        )
        self.assertEqual(
            harness._patch_files(diff),
            {'src/x.ts', 'src/y.ts', 'src/z.ts'}
        )

    def test_patch_files_ignores_dev_null(self):
        """/dev/null references are ignored."""
        diff = (
            '--- a/src/old.ts\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n'
            '--- /dev/null\n+++ b/src/new.ts\n@@ -0,0 +1 @@\n+b'
        )
        self.assertEqual(harness._patch_files(diff), {'src/old.ts', 'src/new.ts'})

    def test_patch_files_strips_ab_prefix(self):
        """a/ and b/ prefixes are stripped."""
        diff = '--- a/lib/util.ts\n+++ b/lib/util.ts\n@@ -1 +1 @@\n-a\n+b'
        self.assertEqual(harness._patch_files(diff), {'lib/util.ts'})

    def test_patch_files_ignores_counter_change_in_git_hunk(self):
        """Changing -- i; to ++ i; must not introduce a file named i;."""
        diff = (
            'diff --git a/lib/counter.ts b/lib/counter.ts\n'
            'index 1234567..abcdef0 100644\n'
            '--- a/lib/counter.ts\n+++ b/lib/counter.ts\n'
            '@@ -1 +1 @@\n--- i;\n+++ i;\n'
        )
        self.assertEqual(harness._patch_files(diff), {'lib/counter.ts'})

    def test_patch_files_ignores_header_paths_inside_multiple_hunks(self):
        """Context and multiple hunks still belong to the same file."""
        diff = (
            '--- a/lib/counter.ts\n+++ b/lib/counter.ts\n'
            '@@ -1,2 +1,3 @@ function count()\n'
            ' context\n--- a/removed.ts\n+++ b/added.ts\n+extra\n'
            '@@ -10,2 +11,2 @@\n--- i;\n+++ i;\n context\n'
        )
        self.assertEqual(harness._patch_files(diff), {'lib/counter.ts'})

    def test_patch_files_reads_real_headers_after_hunk_content(self):
        """Plain unified diffs need no diff --git separator between files."""
        diff = (
            '--- a/src/first.ts\t2026-01-01 00:00:00\n'
            '+++ b/src/first.ts\t2026-01-02 00:00:00\n'
            '@@ -1 +1 @@\n--- a/fake-old.ts\n+++ b/fake-new.ts\n'
            '--- a/src/second.ts\t2026-01-01 00:00:00\n'
            '+++ b/src/second.ts\t2026-01-02 00:00:00\n'
            '@@ -1 +1 @@\n-old\n+new\n'
        )
        self.assertEqual(harness._patch_files(diff), {'src/first.ts', 'src/second.ts'})

    def test_patch_files_ignores_header_content_in_added_and_deleted_files(self):
        """Zero-length hunk ranges distinguish creation and deletion headers."""
        diff = (
            '--- a/src/old.ts\n+++ /dev/null\n'
            '@@ -1,2 +0,0 @@\n--- a/fake-old.ts\n--- i;\n'
            '--- /dev/null\n+++ b/src/new.ts\n'
            '@@ -0,0 +1,2 @@\n+++ b/fake-new.ts\n+++ i;\n'
        )
        self.assertEqual(harness._patch_files(diff), {'src/old.ts', 'src/new.ts'})

    def test_patch_files_no_newline_marker_does_not_end_hunk(self):
        """No-newline markers do not consume either side's hunk range."""
        diff = (
            '--- a/src/first.ts\n+++ b/src/first.ts\n'
            '@@ -1 +1 @@\n--- i;\n'
            '\\ No newline at end of file\n+++ i;\n'
            '\\ No newline at end of file\n'
            '--- a/src/second.ts\n+++ b/src/second.ts\n'
            '@@ -1 +1 @@\n-old\n+new\n'
        )
        self.assertEqual(harness._patch_files(diff), {'src/first.ts', 'src/second.ts'})

    def test_patch_files_preserves_header_only_diff(self):
        """Real headers without hunks keep timestamp and prefix handling."""
        diff = (
            '--- a/src/old name.ts\t2026-01-01 00:00:00\n'
            '+++ b/src/new name.ts\t2026-01-02 00:00:00\n'
        )
        self.assertEqual(harness._patch_files(diff), {'src/old name.ts', 'src/new name.ts'})


class TestRunSmokeSubsetInvalidDevUrl(unittest.TestCase):
    """_run_smoke_subset must not crash when DEV_URL is unreachable."""

    @patch('harness._dev_server_up', return_value=False)
    def test_invalid_dev_url_non_strict_returns_true_tsc_only(self, mock_up):
        """Non-strict mode falls back to tsc-only when dev server is down."""
        with patch.dict(os.environ, {'LOOP_GATE_REQUIRE_E2E': '0'}):
            self.assertTrue(harness._run_smoke_subset('app/login/page.tsx'))

    @patch('harness._dev_server_up', return_value=False)
    def test_invalid_dev_url_strict_returns_false(self, mock_up):
        """Strict mode blocks the fix when dev server is down."""
        with patch.dict(os.environ, {'LOOP_GATE_REQUIRE_E2E': '1'}):
            self.assertFalse(harness._run_smoke_subset('app/login/page.tsx'))

    @patch('harness._dev_server_up', side_effect=Exception('timeout'))
    def test_dev_server_up_exception_not_crashes(self, mock_up):
        """Even if _dev_server_up raises, _run_smoke_subset handles it."""
        with patch.dict(os.environ, {'LOOP_GATE_REQUIRE_E2E': '0'}):
            try:
                harness._run_smoke_subset('app/login/page.tsx')
            except Exception as e:
                self.fail(f'_run_smoke_subset crashed: {e}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
