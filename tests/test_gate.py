#!/usr/bin/env python3
"""Tests del gate de fixes: route-mapping y detección de patch — A-PR5."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402


class TestRouteForFile(unittest.TestCase):
    def test_panel_routes(self):
        self.assertEqual(harness._route_for_file('app/(panel)/despacho/components/X.tsx'), '/despacho')
        self.assertEqual(harness._route_for_file('app/(panel)/caja/ArqueoForm.tsx'), '/caja')
        self.assertEqual(harness._route_for_file('app/(panel)/admin/page.tsx'), '/admin')

    def test_non_panel_route(self):
        self.assertEqual(harness._route_for_file('app/login/page.tsx'), '/login')

    def test_lib_not_routeable(self):
        self.assertIsNone(harness._route_for_file('lib/queries/clientes.ts'))
        self.assertIsNone(harness._route_for_file('lib/db/schema.ts'))


class TestPatchDetection(unittest.TestCase):
    def test_looks_like_patch(self):
        diff = '--- a/x.ts\n+++ b/x.ts\n@@ -1,2 +1,2 @@\n-old\n+new'
        self.assertTrue(harness._looks_like_patch(diff))
        self.assertTrue(harness._looks_like_patch('diff --git a/x b/x\n@@ -1 +1 @@'))

    def test_full_file_not_patch(self):
        self.assertFalse(harness._looks_like_patch('const x = 1;\nexport default x;'))

    def test_patch_files_single(self):
        diff = '--- a/lib/x.ts\n+++ b/lib/x.ts\n@@ -1 +1 @@\n-a\n+b'
        self.assertEqual(harness._patch_files(diff), {'lib/x.ts'})

    def test_patch_files_multi(self):
        diff = ('--- a/lib/x.ts\n+++ b/lib/x.ts\n@@ -1 +1 @@\n-a\n+b\n'
                '--- a/lib/y.ts\n+++ b/lib/y.ts\n@@ -1 +1 @@\n-c\n+d')
        self.assertEqual(harness._patch_files(diff), {'lib/x.ts', 'lib/y.ts'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
