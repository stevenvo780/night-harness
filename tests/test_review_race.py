#!/usr/bin/env python3
"""Stress test for _add_to_review race condition.

_add_to_review (harness.py:1758-1775) has a read-then-write race:
no lock guards the read of REVIEW_FILE + write of REVIEW_FILE. Under
concurrency, two threads may both read the same initial state, append
their items, and write the file — losing some of the items.

This test launches N threads, each adding a unique entry, and asserts
that all N entries are present in the final REVIEW_FILE.

KNOWN RACE: this race is REAL but is currently hidden by the CPython
GIL — short file reads/writes complete in microseconds, so the
window for two threads to interleave is extremely narrow. The test
sometimes passes (when no interleaving happens) and sometimes fails
(massive data loss, sometimes corrupt JSON). On average it passes
because the GIL serializes the bytecode.

The fix is to add a threading.Lock around the read+write in
_add_to_review. We CANNOT modify source from this test, so this test
serves as a regression guard for the future: once a lock is added,
this test will pass deterministically.

Correr:  python3 -m unittest tests.test_review_race -v
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402


# Skip flag: by default, the concurrency test runs and either passes
# (GIL hides the race) or fails (race exposed). Set
# NIGHT_HARNESS_SKIP_RACE=1 to skip just the race test.
SKIP_RACE = os.environ.get('NIGHT_HARNESS_SKIP_RACE', '0') == '1'


def _maybe_skip(test_method):
    """Decorator that skips the test if NIGHT_HARNESS_SKIP_RACE=1."""
    def wrapper(self, *args, **kwargs):
        if SKIP_RACE:
            self.skipTest('race test skipped via NIGHT_HARNESS_SKIP_RACE=1')
        return test_method(self, *args, **kwargs)
    return wrapper


class _ReviewFileFixture(unittest.TestCase):
    """Common setup: redirect REVIEW_FILE to a per-test temp file."""

    def setUp(self):
        self._orig = harness.REVIEW_FILE
        self.tmpdir = tempfile.mkdtemp(prefix='review-race-')
        harness.REVIEW_FILE = Path(self.tmpdir) / 'review.json'

    def tearDown(self):
        harness.REVIEW_FILE = self._orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1) Concurrency stress — documents the known race
# ---------------------------------------------------------------------------

class TestAddToReviewRace(_ReviewFileFixture):
    """20 threads adding unique entries; assert all 20 are present.

    The race in _add_to_review (no lock on read+write) is REAL but
    HIDDEN by the CPython GIL for short file operations. This test
    is a regression guard: if a future change makes the function
    truly concurrent (e.g. moves the read/write to a thread pool,
    or uses async I/O), the race becomes visible and this test
    fails — which is the intended behavior.

    If a future change adds a threading.Lock around the read+write,
    this test will pass deterministically.
    """

    N_THREADS = 20

    @_maybe_skip
    def test_concurrent_add_all_entries_present(self):
        """20 threads × 1 unique entry → final file has all 20 entries.

        On CPython with short file I/O, the GIL typically hides the
        race and all 20 entries land in the file. If this test starts
        failing intermittently, the race has become visible — that's
        the signal to add a threading.Lock in _add_to_review.
        """
        n = self.N_THREADS
        barrier = threading.Barrier(n)
        errors = []

        def worker(i):
            try:
                barrier.wait()  # all threads start at the same time
                sus = harness.Suspect(
                    file=f'unique-{i}.ts',
                    line=i,
                    snippet=f'snippet {i}',
                    why=f'why {i}',
                )
                harness._add_to_review(sus, f'commit-{i}', f'model-{i}')
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [], f'crashes: {errors}')

        # The JSON file may be corrupt if the race was exposed; try
        # to parse it. If corrupt, that's a manifestation of the race.
        try:
            items = json.loads(harness.REVIEW_FILE.read_text())
        except json.JSONDecodeError as e:
            self.fail(
                f'KNOWN RACE: REVIEW_FILE contains invalid JSON due to '
                f'concurrent read+write in _add_to_review. {e}. '
                f'Add a threading.Lock around the read+write to fix.'
            )
        files = {item['file'] for item in items}
        expected = {f'unique-{i}.ts' for i in range(n)}
        missing = expected - files
        self.assertEqual(
            missing, set(),
            f'KNOWN RACE: lost {len(missing)}/{n} entries due to '
            f'read+write race in _add_to_review. Add a threading.Lock '
            f'around the read+write to fix. Missing: {sorted(missing)}',
        )


# ---------------------------------------------------------------------------
# 2) Single-threaded sanity check (always passes; useful for isolation)
# ---------------------------------------------------------------------------

class TestAddToReviewSingleThreaded(_ReviewFileFixture):
    """Baseline: single-threaded calls work correctly."""

    def test_sequential_add_all_entries_present(self):
        """20 sequential calls → all 20 entries present (no race in serial)."""
        n = 20
        for i in range(n):
            sus = harness.Suspect(
                file=f'seq-{i}.ts',
                line=i,
                snippet=f'snippet {i}',
                why=f'why {i}',
            )
            harness._add_to_review(sus, f'commit-{i}', f'model-{i}')
        items = json.loads(harness.REVIEW_FILE.read_text())
        self.assertEqual(len(items), n)
        files = {item['file'] for item in items}
        self.assertEqual(files, {f'seq-{i}.ts' for i in range(n)})

    def test_sequential_preserves_order(self):
        """Entries appear in insertion order when called sequentially."""
        for i in range(5):
            sus = harness.Suspect(
                file=f'f{i}.ts', line=i, snippet='x', why='y',
            )
            harness._add_to_review(sus, f'c{i}', 'm')
        items = json.loads(harness.REVIEW_FILE.read_text())
        for i, item in enumerate(items):
            self.assertEqual(item['file'], f'f{i}.ts')


# ---------------------------------------------------------------------------
# 3) Sequential smoke (always passes; documents what "good" looks like)
# ---------------------------------------------------------------------------

class TestAddToReviewContract(_ReviewFileFixture):
    """Contract tests for _add_to_review's serialized behavior."""

    def test_each_entry_has_required_fields(self):
        """Every entry has commit, file, line, why, snippet, model."""
        sus = harness.Suspect(
            file='app.ts', line=10, snippet='const x = 1;', why='why',
        )
        harness._add_to_review(sus, 'commit-X', 'model-Y')
        items = json.loads(harness.REVIEW_FILE.read_text())
        self.assertEqual(len(items), 1)
        item = items[0]
        for field in ('commit', 'file', 'line', 'why', 'snippet', 'model'):
            self.assertIn(field, item, f'missing field: {field}')

    def test_reason_field_present(self):
        """Each entry has a 'reason' field explaining the review."""
        sus = harness.Suspect(file='x.ts', line=1, snippet='x', why='y')
        harness._add_to_review(sus, 'c', 'm')
        items = json.loads(harness.REVIEW_FILE.read_text())
        self.assertIn('reason', items[0])
        self.assertIn('revisi', items[0]['reason'].lower())


if __name__ == '__main__':
    unittest.main(verbosity=2)
