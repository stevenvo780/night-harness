#!/usr/bin/env python3
"""Real-filesystem tests for discover_code_zones.

The existing test_discover_code_zones (in test_fixer_flow.py) uses
_make_node mocks. This file exercises discover_code_zones against a
real TemporaryDirectory with a realistic project layout:

  apps/dashboard/
    app/(panel)/zone-a/page.tsx, components/Table.tsx
    app/(panel)/zone-b/page.tsx
    lib/queries/users.ts
    README.md
    package.json
    .git/HEAD                  (hidden dir)
    app/(panel)/zone-c/__pycache__/foo.ts  (pycache)

The tests document the actual behavior:
  · Hidden directories are NOT excluded (the code only filters
    paren groups `(<name>)`).
  · __pycache__ is NOT excluded but doesn't contain .ts files,
    so it has no effect.
  · Non-source files (.md, .json) are excluded by virtue of the
    `rglob('*.ts')` / `rglob('*.tsx')` patterns.
  · Empty directories produce no zones.

Correr:  python3 -m unittest tests.test_discovery -v
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402


class _WorktreeFixture(unittest.TestCase):
    """Common setup: redirect WT/SUBDIR/WT_SUBDIR to a temp directory."""

    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmpdir_obj.name)
        self._orig_wt = harness.WT
        self._orig_subdir = harness.SUBDIR
        self._orig_wtsub = harness.WT_SUBDIR
        harness.WT = self.tmpdir
        harness.SUBDIR = 'apps/dashboard'
        harness.WT_SUBDIR = self.tmpdir / 'apps/dashboard'
        (harness.WT_SUBDIR).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        harness.WT = self._orig_wt
        harness.SUBDIR = self._orig_subdir
        harness.WT_SUBDIR = self._orig_wtsub
        self.tmpdir_obj.cleanup()

    def _make_zones(self, structure: dict):
        """Create a project structure from a nested dict.

        Each key is a path; if the value is a dict, it's a directory
        and we recurse; if the value is a string, we write a file with
        that content.
        """
        for path, val in structure.items():
            full = self.tmpdir / path
            if isinstance(val, dict):
                full.mkdir(parents=True, exist_ok=True)
                self._make_zones({f'{path}/{k}': v for k, v in val.items()})
            else:
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_text(val)


# ---------------------------------------------------------------------------
# 1) Realistic structure
# ---------------------------------------------------------------------------

class TestDiscoverRealFilesystem(_WorktreeFixture):
    """Realistic structure: 3 modules, 5 files in each, 2-3 subdirs."""

    def test_discovers_all_three_modules(self):
        """3 modules with .ts/.tsx files → 3 zones in the result."""
        structure = {
            'apps/dashboard/app/(panel)/alpha/page.tsx': 'export const x = 1;',
            'apps/dashboard/app/(panel)/alpha/comp/Table.tsx': 'export const t = 1;',
            'apps/dashboard/app/(panel)/alpha/utils.ts': 'export const u = 1;',
            'apps/dashboard/app/(panel)/alpha/sub/inner.tsx': 'export const i = 1;',
            'apps/dashboard/app/(panel)/alpha/sub/deep/leaf.ts': 'export const l = 1;',
            'apps/dashboard/app/(panel)/beta/page.tsx': 'export const x = 2;',
            'apps/dashboard/app/(panel)/beta/cards.tsx': 'export const c = 2;',
            'apps/dashboard/app/(panel)/gamma/page.tsx': 'export const x = 3;',
            'apps/dashboard/lib/queries/users.ts': 'export const u = 1;',
            'apps/dashboard/lib/queries/orders.ts': 'export const o = 1;',
        }
        self._make_zones(structure)
        groups = harness.discover_code_zones()
        # Flatten and count unique files.
        flat = [f for g in groups for f in g]
        # All 10 source files should be discovered.
        self.assertEqual(len(flat), 10)
        # Spot-check that each file is in some group.
        for path in [
            'app/(panel)/alpha/page.tsx',
            'app/(panel)/alpha/sub/deep/leaf.ts',
            'app/(panel)/beta/cards.tsx',
            'lib/queries/users.ts',
        ]:
            self.assertIn(path, flat, f'{path} not in discovered zones')

    def test_zone_count_correct(self):
        """3 panel zones + 1 lib prefix = at least 4 distinct groups.

        Note: lib/queries becomes a single group 'lib/queries/'.
        """
        structure = {
            'apps/dashboard/app/(panel)/a/page.tsx': 'x',
            'apps/dashboard/app/(panel)/a/sub.tsx': 'x',
            'apps/dashboard/app/(panel)/b/page.tsx': 'x',
            'apps/dashboard/app/(panel)/c/page.tsx': 'x',
            'apps/dashboard/lib/db/conn.ts': 'x',
            'apps/dashboard/lib/db/migrate.ts': 'x',
        }
        self._make_zones(structure)
        groups = harness.discover_code_zones()
        # Count distinct zone prefixes.
        prefixes = set()
        for g in groups:
            for f in g:
                # Panel zone: app/(panel)/<name>/
                if 'app/(panel)/' in f:
                    parts = f.split('app/(panel)/')[1].split('/')
                    prefixes.add(f'app/(panel)/{parts[0]}/')
                else:
                    # lib/<prefix>/
                    parts = f.split('/')
                    prefixes.add('/'.join(parts[:2]) + '/')
        # 3 panel zones + 1 lib prefix.
        self.assertIn('app/(panel)/a/', prefixes)
        self.assertIn('app/(panel)/b/', prefixes)
        self.assertIn('app/(panel)/c/', prefixes)
        self.assertIn('lib/db/', prefixes)


# ---------------------------------------------------------------------------
# 2) Hidden directories
# ---------------------------------------------------------------------------

class TestHiddenDirectories(_WorktreeFixture):
    """Document the actual behavior of hidden directories.

    The current code only excludes panel subdirs starting with '('
    (paren groups). Hidden directories like .git, .next, __pycache__
    are NOT excluded. This is a known quirk — if a future change
    filters them out, this test will need updating.
    """

    def test_hidden_dirs_under_panel_are_included(self):
        """.git, .next, etc. under app/(panel)/ are NOT excluded."""
        structure = {
            'apps/dashboard/app/(panel)/.git/HEAD': 'ref: refs/heads/main',
            'apps/dashboard/app/(panel)/.next/cache/file.ts': 'x',
            'apps/dashboard/app/(panel)/real/page.tsx': 'x',
        }
        self._make_zones(structure)
        groups = harness.discover_code_zones()
        flat = [f for g in groups for f in g]
        # .next/cache/file.ts is included (hidden dirs not filtered).
        self.assertIn('app/(panel)/.next/cache/file.ts', flat)
        # The real zone is also included.
        self.assertIn('app/(panel)/real/page.tsx', flat)

    def test_pycache_under_panel_no_effect(self):
        """__pycache__/foo.ts is included if it exists, but typically empty."""
        structure = {
            'apps/dashboard/app/(panel)/real/page.tsx': 'x',
            # If __pycache__ has a .ts file, it would be included.
            'apps/dashboard/app/(panel)/real/__pycache__/foo.ts': 'x',
        }
        self._make_zones(structure)
        groups = harness.discover_code_zones()
        flat = [f for g in groups for f in g]
        # __pycache__ dir is included since it's not a paren group.
        self.assertIn('app/(panel)/real/__pycache__/foo.ts', flat)


# ---------------------------------------------------------------------------
# 3) Non-source files
# ---------------------------------------------------------------------------

class TestNonSourceFiles(_WorktreeFixture):
    """Non-source files (.md, .json) are excluded."""

    def test_md_json_excluded(self):
        """Only .ts and .tsx files are included; .md and .json are skipped."""
        structure = {
            'apps/dashboard/app/(panel)/real/page.tsx': 'x',
            'apps/dashboard/app/(panel)/real/README.md': '# hello',
            'apps/dashboard/app/(panel)/real/data.json': '{}',
            'apps/dashboard/app/(panel)/real/pkg/package.json': '{}',
            'apps/dashboard/README.md': '# root',
            'apps/dashboard/package.json': '{}',
        }
        self._make_zones(structure)
        groups = harness.discover_code_zones()
        flat = [f for g in groups for f in g]
        # .tsx is included.
        self.assertIn('app/(panel)/real/page.tsx', flat)
        # .md and .json are excluded.
        for excluded in [
            'app/(panel)/real/README.md',
            'app/(panel)/real/data.json',
            'app/(panel)/real/pkg/package.json',
            'README.md',
            'package.json',
        ]:
            self.assertNotIn(excluded, flat,
                             f'{excluded} should be excluded')

    def test_only_tsx_in_dir(self):
        """A panel subdir with only .tsx files → still a zone."""
        structure = {
            'apps/dashboard/app/(panel)/tsxonly/comp.tsx': 'x',
            'apps/dashboard/app/(panel)/tsxonly/other.tsx': 'x',
        }
        self._make_zones(structure)
        groups = harness.discover_code_zones()
        flat = [f for g in groups for f in g]
        self.assertIn('app/(panel)/tsxonly/comp.tsx', flat)
        self.assertIn('app/(panel)/tsxonly/other.tsx', flat)

    def test_only_ts_no_tsx(self):
        """A panel subdir with only .ts files → still a zone."""
        structure = {
            'apps/dashboard/app/(panel)/tsonly/a.ts': 'x',
            'apps/dashboard/app/(panel)/tsonly/b.ts': 'x',
        }
        self._make_zones(structure)
        groups = harness.discover_code_zones()
        flat = [f for g in groups for f in g]
        self.assertIn('app/(panel)/tsonly/a.ts', flat)
        self.assertIn('app/(panel)/tsonly/b.ts', flat)


# ---------------------------------------------------------------------------
# 4) Empty directories
# ---------------------------------------------------------------------------

class TestEmptyDirectories(_WorktreeFixture):
    """Empty directories produce no zones."""

    def test_empty_panel_subdir_no_zone(self):
        """app/(panel)/empty/ with no .ts files → not in result."""
        structure = {
            'apps/dashboard/app/(panel)/empty/.gitkeep': '',
            'apps/dashboard/app/(panel)/real/page.tsx': 'x',
        }
        self._make_zones(structure)
        groups = harness.discover_code_zones()
        flat = [f for g in groups for f in g]
        self.assertNotIn('app/(panel)/empty/.gitkeep', flat)
        self.assertIn('app/(panel)/real/page.tsx', flat)

    def test_empty_lib_dir_no_zone(self):
        """lib/empty/ with no .ts files → no group 'lib/empty/'."""
        structure = {
            'apps/dashboard/lib/queries/users.ts': 'x',
            'apps/dashboard/lib/empty/.gitkeep': '',
        }
        self._make_zones(structure)
        groups = harness.discover_code_zones()
        flat = [f for g in groups for f in g]
        # Only lib/queries has .ts files.
        self.assertIn('lib/queries/users.ts', flat)
        # No entries from lib/empty/.
        for f in flat:
            self.assertFalse(
                f.startswith('lib/empty/'),
                f'unexpected file in lib/empty/: {f}',
            )

    def test_all_empty_returns_empty(self):
        """An empty worktree → empty result."""
        # Just the bare worktree, no .ts files anywhere.
        groups = harness.discover_code_zones()
        self.assertEqual(groups, [])


# ---------------------------------------------------------------------------
# 5) Mixed content
# ---------------------------------------------------------------------------

class TestMixedContent(_WorktreeFixture):
    """Mixed real + non-source + hidden files."""

    def test_mixed_realistic_project(self):
        """A realistic dashboard with mixed file types."""
        structure = {
            # Panel zones with real source.
            'apps/dashboard/app/(panel)/clientes/page.tsx': 'x',
            'apps/dashboard/app/(panel)/clientes/Table.tsx': 'x',
            'apps/dashboard/app/(panel)/clientes/test.tsx': 'x',
            'apps/dashboard/app/(panel)/cobros/page.tsx': 'x',
            # Hidden dirs (not excluded).
            'apps/dashboard/app/(panel)/.next/cache.ts': 'x',
            'apps/dashboard/.git/HEAD': 'x',
            # Non-source files (excluded).
            'apps/dashboard/app/(panel)/clientes/README.md': 'x',
            'apps/dashboard/app/(panel)/clientes/data.json': 'x',
            # lib/ with mixed content.
            'apps/dashboard/lib/queries/users.ts': 'x',
            'apps/dashboard/lib/queries/users.test.ts': 'x',  # test excluded
            'apps/dashboard/lib/queries/README.md': 'x',  # md excluded
            'apps/dashboard/lib/empty/.gitkeep': '',  # empty, no .ts
        }
        self._make_zones(structure)
        groups = harness.discover_code_zones()
        flat = [f for g in groups for f in g]
        # Real source files present.
        for expected in [
            'app/(panel)/clientes/page.tsx',
            'app/(panel)/cobros/page.tsx',
            'lib/queries/users.ts',
            'app/(panel)/.next/cache.ts',  # hidden but included
        ]:
            self.assertIn(expected, flat, f'missing: {expected}')
        # Excluded files absent.
        for excluded in [
            'app/(panel)/clientes/README.md',
            'app/(panel)/clientes/data.json',
            'lib/queries/users.test.ts',  # .test.ts excluded
            'lib/queries/README.md',
        ]:
            self.assertNotIn(excluded, flat, f'should be excluded: {excluded}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
