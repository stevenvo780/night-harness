#!/usr/bin/env python3
"""Tests del módulo de memoria persistente (memory.py) del Harness v6.

Cubre el hash estable, dataclasses (BugRecord/FalsePositive/ZoneHardness),
persistencia atómica load/save JSON, ciclo de vida de bugs (record/get/open),
falsos positivos con threshold de skip, endurecimiento de zonas (clean_rounds,
is_hardened, priority_for_zone) y resumen para reportes. Termina con un test
de concurrencia para confirmar que `record_bug` no corrompe el estado bajo
carga de hilos.

Correr:  python3 -m unittest tests.test_memory -v
"""
import dataclasses
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import memory  # noqa: E402


# ---------------------------------------------------------------------------
# 1) hash_suspect
# ---------------------------------------------------------------------------
class TestHashSuspect(unittest.TestCase):
    """El hash es estable, de 16 chars hex, y depende solo de file+line+primeras 8 palabras."""

    def test_returns_sixteen_char_hex(self):
        h = memory.hash_suspect('a.py', 1, 'x = 1')
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 16)
        self.assertTrue(all(c in '0123456789abcdef' for c in h), f'no hex: {h!r}')

    def test_same_inputs_same_hash(self):
        a = memory.hash_suspect('a.py', 10, 'foo bar')
        b = memory.hash_suspect('a.py', 10, 'foo bar')
        self.assertEqual(a, b)

    def test_different_line_different_hash(self):
        a = memory.hash_suspect('a.py', 1, 'foo bar')
        b = memory.hash_suspect('a.py', 2, 'foo bar')
        self.assertNotEqual(a, b)

    def test_different_file_different_hash(self):
        a = memory.hash_suspect('a.py', 1, 'foo bar')
        b = memory.hash_suspect('b.py', 1, 'foo bar')
        self.assertNotEqual(a, b)

    def test_different_first_eight_words_different_hash(self):
        a = memory.hash_suspect('a.py', 1, 'one two three four five six seven eight')
        b = memory.hash_suspect('a.py', 1, 'alpha beta gamma delta epsilon zeta eta theta')
        self.assertNotEqual(a, b)

    def test_hash_stable_when_snippet_has_more_than_eight_words(self):
        """El 9º palabra y siguientes NO afectan el hash."""
        short = 'one two three four five six seven eight'
        long = short + ' nine ten eleven twelve'
        a = memory.hash_suspect('a.py', 1, short)
        b = memory.hash_suspect('a.py', 1, long)
        self.assertEqual(a, b)

    def test_hash_stable_with_trailing_whitespace(self):
        """Whitespace distinto al final no debería cambiar el hash porque split() lo colapsa."""
        a = memory.hash_suspect('a.py', 1, 'foo bar')
        b = memory.hash_suspect('a.py', 1, 'foo bar   ')
        self.assertEqual(a, b)

    def test_hash_independent_of_extra_spaces_inside_first_eight(self):
        """split() colapsa espacios, así que 'foo  bar' == 'foo bar'."""
        a = memory.hash_suspect('a.py', 1, 'foo bar')
        b = memory.hash_suspect('a.py', 1, 'foo  bar')
        self.assertEqual(a, b)


# ---------------------------------------------------------------------------
# 2) Persistencia: Memory.load / Memory.save (atomic, JSON válido)
# ---------------------------------------------------------------------------
class TestMemoryLoadSave(unittest.TestCase):
    """load() debe tolerar archivos faltantes o corruptos; save() debe ser atómico."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memory-test-')
        self.path = Path(self.tmpdir) / 'memory.json'

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_missing_file_returns_empty(self):
        mem = memory.Memory.load(path=self.path)
        self.assertIsInstance(mem, memory.Memory)
        self.assertEqual(mem.bugs, {})
        self.assertEqual(mem.false_positives, {})
        self.assertEqual(mem.zones, {})
        self.assertEqual(mem.total_rounds, 0)
        self.assertEqual(mem.total_bugs_found, 0)
        self.assertEqual(mem.total_fixes_applied, 0)
        self.assertEqual(mem.last_session_at, 0.0)
        self.assertEqual(mem.last_session_summary, '')

    def test_load_corrupted_json_returns_empty(self):
        self.path.write_text('{ this is not valid json')
        mem = memory.Memory.load(path=self.path)
        self.assertIsInstance(mem, memory.Memory)
        self.assertEqual(mem.bugs, {})
        self.assertEqual(mem.false_positives, {})
        self.assertEqual(mem.zones, {})

    def test_load_empty_file_returns_empty(self):
        self.path.write_text('')
        mem = memory.Memory.load(path=self.path)
        self.assertIsInstance(mem, memory.Memory)
        self.assertEqual(mem.bugs, {})

    def test_load_truncated_json_returns_empty(self):
        self.path.write_text('{"bugs": {"abc":')
        mem = memory.Memory.load(path=self.path)
        self.assertEqual(mem.bugs, {})

    def test_save_writes_valid_json(self):
        mem = memory.Memory()
        mem.total_rounds = 5
        mem.total_bugs_found = 3
        mem.save(path=self.path)
        self.assertTrue(self.path.exists())
        data = json.loads(self.path.read_text())
        self.assertIsInstance(data, dict)
        self.assertEqual(data['total_rounds'], 5)
        self.assertEqual(data['total_bugs_found'], 3)
        self.assertIn('bugs', data)
        self.assertIn('false_positives', data)
        self.assertIn('zones', data)

    def test_save_is_atomic_no_tmp_left(self):
        """Después de save() no debe quedar el archivo .tmp en disco."""
        mem = memory.Memory()
        mem.save(path=self.path)
        self.assertTrue(self.path.exists())
        # El tmp es path.with_suffix('.tmp') → memory.tmp
        tmp = self.path.with_suffix('.tmp')
        self.assertFalse(tmp.exists(), f'save() dejó un .tmp: {tmp}')

    def test_save_uses_replace_not_direct_write(self):
        """Spy sobre Path.replace: save() debe llamarlo una vez con un .tmp como source."""
        mem = memory.Memory()
        original_replace = Path.replace
        called = []

        def spy_replace(self, target):
            called.append((str(self), str(target)))
            return original_replace(self, target)

        Path.replace = spy_replace
        try:
            mem.save(path=self.path)
        finally:
            Path.replace = original_replace
        self.assertEqual(len(called), 1, f'replace() debería llamarse 1 vez, fue {len(called)}')
        src, tgt = called[0]
        self.assertEqual(tgt, str(self.path))
        self.assertTrue(src.endswith('.tmp'), f'source de replace debería ser .tmp, fue {src}')

    def test_save_creates_parent_dirs(self):
        nested = Path(self.tmpdir) / 'a' / 'b' / 'c' / 'memory.json'
        mem = memory.Memory()
        mem.save(path=nested)
        self.assertTrue(nested.exists())

    def test_roundtrip_save_load_preserves_state(self):
        """save() → load() debe reconstruir el mismo estado."""
        mem = memory.Memory()
        mem.total_rounds = 7
        mem.total_fixes_applied = 2
        mem.last_session_summary = 'session 1 — clean'
        mem.last_session_at = time.time()
        mem.record_bug('a.py', 1, 'foo bar baz')           # +1 bug
        mem.record_bug('a.py', 1, 'foo bar baz')           # misma hash, times_confirmed++
        mem.record_bug('a.py', 2, 'second bug content')    # +1 bug
        mem.record_bug('a.py', 2, 'second bug content',
                      fix_commit='deadbeef')               # +1 fix
        mem.record_false_positive('b.py', 5, 'noise pattern', reason='flaky test')
        mem.mark_zone_clean('a.py')
        mem.mark_zone_clean('a.py')
        mem.save(path=self.path)

        loaded = memory.Memory.load(path=self.path)
        self.assertEqual(loaded.total_rounds, 7)
        self.assertEqual(loaded.total_bugs_found, 2)
        self.assertEqual(loaded.total_fixes_applied, 3)  # 2 manuales + 1 del record
        self.assertEqual(loaded.last_session_summary, 'session 1 — clean')
        self.assertEqual(len(loaded.bugs), 2)
        self.assertEqual(len(loaded.false_positives), 1)
        self.assertIn('a.py', loaded.zones)
        self.assertEqual(loaded.zones['a.py'].clean_rounds, 2)
        # El bug de a.py:1 tiene times_confirmed=2
        rec1 = loaded.get_bug('a.py', 1, 'foo bar baz')
        self.assertEqual(rec1.times_confirmed, 2)
        # El bug de a.py:2 está fixed con su fix_commit
        rec2 = loaded.get_bug('a.py', 2, 'second bug content')
        self.assertTrue(rec2.fixed)
        self.assertEqual(rec2.fix_commit, 'deadbeef')

    def test_load_default_path_uses_memory_path(self):
        """Memory.load() sin argumentos usa MEMORY_PATH (constante del módulo)."""
        # No escribimos en MEMORY_PATH real; solo verificamos el contrato.
        self.assertIsNotNone(memory.MEMORY_PATH)
        self.assertIsInstance(memory.MEMORY_PATH, Path)

    def test_save_default_path_uses_memory_path(self):
        """Memory.save() sin argumentos usa MEMORY_PATH."""
        # No escribimos nada real; verificamos que el método acepta el default.
        mem = memory.Memory()
        self.assertEqual(mem.save.__defaults__[0], memory.MEMORY_PATH)


# ---------------------------------------------------------------------------
# 3) BugRecord + record_bug / get_bug / open_bugs
# ---------------------------------------------------------------------------
class TestBugTracking(unittest.TestCase):
    """Ciclo de vida de un bug: alta, recurrencia, fix, listado de abiertos."""

    def setUp(self):
        self.mem = memory.Memory()

    def test_record_bug_new_adds_bugrecord(self):
        rec = self.mem.record_bug('a.py', 1, 'foo bar')
        self.assertIsInstance(rec, memory.BugRecord)
        self.assertEqual(rec.file, 'a.py')
        self.assertEqual(rec.line, 1)
        self.assertEqual(rec.times_confirmed, 1)
        self.assertFalse(rec.fixed)
        self.assertIsNone(rec.fix_commit)
        self.assertEqual(len(self.mem.bugs), 1)
        self.assertEqual(self.mem.total_bugs_found, 1)

    def test_record_bug_same_hash_increments_times_confirmed(self):
        self.mem.record_bug('a.py', 1, 'foo bar')
        rec = self.mem.record_bug('a.py', 1, 'foo bar')
        self.assertEqual(rec.times_confirmed, 2)
        self.assertEqual(len(self.mem.bugs), 1)
        self.assertEqual(self.mem.total_bugs_found, 1)  # no incrementa en recurrencia

    def test_record_bug_three_times_increments_to_three(self):
        for _ in range(3):
            self.mem.record_bug('a.py', 1, 'foo bar')
        rec = self.mem.get_bug('a.py', 1, 'foo bar')
        self.assertEqual(rec.times_confirmed, 3)

    def test_record_bug_existing_with_fix_commit_marks_fixed(self):
        self.mem.record_bug('a.py', 1, 'foo bar')
        rec = self.mem.record_bug('a.py', 1, 'foo bar', fix_commit='abc1234')
        self.assertTrue(rec.fixed)
        self.assertEqual(rec.fix_commit, 'abc1234')
        self.assertEqual(self.mem.total_fixes_applied, 1)

    def test_record_bug_new_with_fix_commit_marks_fixed(self):
        rec = self.mem.record_bug('a.py', 1, 'foo bar', fix_commit='deadbeef')
        self.assertTrue(rec.fixed)
        self.assertEqual(rec.fix_commit, 'deadbeef')

    def test_record_bug_updates_last_seen(self):
        rec1 = self.mem.record_bug('a.py', 1, 'foo bar')
        time.sleep(0.005)
        rec2 = self.mem.record_bug('a.py', 1, 'foo bar')
        self.assertGreaterEqual(rec2.last_seen, rec1.last_seen)

    def test_record_bug_different_lines_tracked_separately(self):
        self.mem.record_bug('a.py', 1, 'foo bar')
        self.mem.record_bug('a.py', 2, 'foo bar')
        self.assertEqual(len(self.mem.bugs), 2)
        self.assertEqual(self.mem.total_bugs_found, 2)

    def test_record_bug_snippet_truncated_to_300(self):
        long = 'word ' * 200  # 1000 chars
        rec = self.mem.record_bug('a.py', 1, long)
        self.assertLessEqual(len(rec.snippet), 300)
        self.assertTrue(rec.snippet.startswith('word word word'))

    def test_get_bug_existing_returns_bugrecord(self):
        self.mem.record_bug('a.py', 1, 'foo bar')
        rec = self.mem.get_bug('a.py', 1, 'foo bar')
        self.assertIsNotNone(rec)
        self.assertEqual(rec.file, 'a.py')
        self.assertEqual(rec.line, 1)

    def test_get_bug_unknown_returns_none(self):
        self.assertIsNone(self.mem.get_bug('nope.py', 99, 'nothing here'))

    def test_get_bug_different_snippet_returns_none(self):
        self.mem.record_bug('a.py', 1, 'foo bar')
        self.assertIsNone(self.mem.get_bug('a.py', 1, 'totally different content'))

    def test_open_bugs_filters_out_fixed(self):
        self.mem.record_bug('a.py', 1, 'first bug content')
        self.mem.record_bug('a.py', 2, 'second bug content')
        # Marcar el segundo como fixed vía re-record con fix_commit.
        self.mem.record_bug('a.py', 2, 'second bug content', fix_commit='deadbeef')
        open_bugs = self.mem.open_bugs()
        self.assertEqual(len(open_bugs), 1)
        self.assertEqual(open_bugs[0].line, 1)
        self.assertFalse(open_bugs[0].fixed)

    def test_open_bugs_empty_when_no_bugs(self):
        self.assertEqual(self.mem.open_bugs(), [])

    def test_open_bugs_empty_when_all_fixed(self):
        self.mem.record_bug('a.py', 1, 'foo bar', fix_commit='x1')
        self.mem.record_bug('a.py', 2, 'baz qux', fix_commit='x2')
        self.assertEqual(self.mem.open_bugs(), [])

    def test_bugrecord_has_expected_fields(self):
        rec = self.mem.record_bug('a.py', 1, 'foo bar')
        field_names = {f.name for f in dataclasses.fields(memory.BugRecord)}
        expected = {'hash', 'file', 'line', 'snippet', 'first_seen',
                    'last_seen', 'times_confirmed', 'fixed', 'fix_commit'}
        self.assertTrue(expected.issubset(field_names),
                        f'faltan campos: {expected - field_names}')
        # Invariantes de valor:
        self.assertEqual(len(rec.hash), 16)
        self.assertGreater(rec.first_seen, 0)
        self.assertGreaterEqual(rec.last_seen, rec.first_seen)


# ---------------------------------------------------------------------------
# 4) Falsos positivos y should_skip
# ---------------------------------------------------------------------------
class TestFalsePositives(unittest.TestCase):
    """Threshold de skip: debe ser ≥2 vistas para descartar."""

    def setUp(self):
        self.mem = memory.Memory()

    def test_record_fp_new_adds(self):
        fp = self.mem.record_false_positive('a.py', 1, 'foo bar', reason='test flake')
        self.assertIsInstance(fp, memory.FalsePositive)
        self.assertEqual(fp.times_seen, 1)
        self.assertEqual(fp.reason, 'test flake')
        self.assertEqual(len(self.mem.false_positives), 1)

    def test_record_fp_same_increments_times_seen(self):
        self.mem.record_false_positive('a.py', 1, 'foo bar')
        fp = self.mem.record_false_positive('a.py', 1, 'foo bar')
        self.assertEqual(fp.times_seen, 2)
        self.assertEqual(len(self.mem.false_positives), 1)

    def test_record_fp_three_times_increments_to_three(self):
        for _ in range(3):
            self.mem.record_false_positive('a.py', 1, 'foo bar')
        fp = self.mem.false_positives[
            memory.hash_suspect('a.py', 1, 'foo bar')
        ]
        self.assertEqual(fp.times_seen, 3)

    def test_record_fp_default_reason_empty(self):
        fp = self.mem.record_false_positive('a.py', 1, 'foo bar')
        self.assertEqual(fp.reason, '')

    def test_should_skip_new_suspect_returns_false(self):
        self.assertFalse(self.mem.should_skip('a.py', 1, 'foo bar'))

    def test_should_skip_after_one_record_returns_false(self):
        """Threshold es 2: una sola vista NO descarta."""
        self.mem.record_false_positive('a.py', 1, 'foo bar')
        self.assertFalse(self.mem.should_skip('a.py', 1, 'foo bar'))

    def test_should_skip_after_two_records_returns_true(self):
        self.mem.record_false_positive('a.py', 1, 'foo bar')
        self.mem.record_false_positive('a.py', 1, 'foo bar')
        self.assertTrue(self.mem.should_skip('a.py', 1, 'foo bar'))

    def test_should_skip_unrelated_suspect_returns_false(self):
        self.mem.record_false_positive('a.py', 1, 'foo bar')
        self.mem.record_false_positive('a.py', 1, 'foo bar')
        self.assertFalse(self.mem.should_skip('b.py', 5, 'different content'))


# ---------------------------------------------------------------------------
# 5) Endurecimiento de zonas: mark_zone_clean, is_hardened
# ---------------------------------------------------------------------------
class TestZoneHardness(unittest.TestCase):
    """clean_rounds crece en cada mark; is_hardened respeta threshold."""

    def setUp(self):
        self.mem = memory.Memory()

    def test_mark_zone_clean_first_time_initializes(self):
        self.mem.mark_zone_clean('a.py')
        self.assertIn('a.py', self.mem.zones)
        self.assertIsInstance(self.mem.zones['a.py'], memory.ZoneHardness)
        self.assertEqual(self.mem.zones['a.py'].file, 'a.py')
        self.assertEqual(self.mem.zones['a.py'].clean_rounds, 1)

    def test_mark_zone_clean_increments(self):
        self.mem.mark_zone_clean('a.py')
        self.mem.mark_zone_clean('a.py')
        self.mem.mark_zone_clean('a.py')
        self.assertEqual(self.mem.zones['a.py'].clean_rounds, 3)

    def test_mark_zone_clean_updates_last_clean(self):
        before = time.time()
        self.mem.mark_zone_clean('a.py')
        z = self.mem.zones['a.py']
        self.assertGreaterEqual(z.last_clean, before)

    def test_is_hardened_after_threshold(self):
        for _ in range(3):
            self.mem.mark_zone_clean('a.py')
        self.assertTrue(self.mem.is_hardened('a.py'))

    def test_is_hardened_default_threshold_is_three(self):
        """is_hardened default = 3 rondas."""
        for _ in range(2):
            self.mem.mark_zone_clean('a.py')
        self.assertFalse(self.mem.is_hardened('a.py'))
        self.mem.mark_zone_clean('a.py')
        self.assertTrue(self.mem.is_hardened('a.py'))

    def test_is_hardened_below_threshold(self):
        self.mem.mark_zone_clean('a.py')
        self.mem.mark_zone_clean('a.py')
        self.assertFalse(self.mem.is_hardened('a.py'))

    def test_is_hardened_unknown_zone_returns_false(self):
        self.assertFalse(self.mem.is_hardened('unknown.py'))

    def test_is_hardened_custom_threshold(self):
        for _ in range(5):
            self.mem.mark_zone_clean('a.py')
        self.assertTrue(self.mem.is_hardened('a.py', threshold=3))
        self.assertTrue(self.mem.is_hardened('a.py', threshold=5))
        self.assertFalse(self.mem.is_hardened('a.py', threshold=10))

    def test_different_zones_independent(self):
        self.mem.mark_zone_clean('a.py')
        self.mem.mark_zone_clean('a.py')
        self.mem.mark_zone_clean('a.py')
        self.mem.mark_zone_clean('b.py')
        self.assertEqual(self.mem.zones['a.py'].clean_rounds, 3)
        self.assertEqual(self.mem.zones['b.py'].clean_rounds, 1)
        self.assertTrue(self.mem.is_hardened('a.py'))
        self.assertFalse(self.mem.is_hardened('b.py'))


# ---------------------------------------------------------------------------
# 6) priority_for_zone
# ---------------------------------------------------------------------------
class TestPriority(unittest.TestCase):
    """0.0 (endurecida) .. 1.0 (máxima prioridad); 0.5 default para zonas nuevas."""

    def setUp(self):
        self.mem = memory.Memory()

    def test_new_zone_returns_half(self):
        self.assertEqual(self.mem.priority_for_zone('a.py'), 0.5)

    def test_not_yet_hardened_one_round(self):
        self.mem.mark_zone_clean('a.py')
        self.assertEqual(self.mem.priority_for_zone('a.py'), 0.6)

    def test_not_yet_hardened_two_rounds(self):
        self.mem.mark_zone_clean('a.py')
        self.mem.mark_zone_clean('a.py')
        self.assertEqual(self.mem.priority_for_zone('a.py'), 0.6)

    def test_soft_hardened_three_rounds(self):
        for _ in range(3):
            self.mem.mark_zone_clean('a.py')
        self.assertEqual(self.mem.priority_for_zone('a.py'), 0.1)

    def test_soft_hardened_four_rounds(self):
        for _ in range(4):
            self.mem.mark_zone_clean('a.py')
        self.assertEqual(self.mem.priority_for_zone('a.py'), 0.1)

    def test_hardened_five_rounds(self):
        for _ in range(5):
            self.mem.mark_zone_clean('a.py')
        self.assertEqual(self.mem.priority_for_zone('a.py'), 0.0)

    def test_hardened_many_rounds_still_zero(self):
        for _ in range(20):
            self.mem.mark_zone_clean('a.py')
        self.assertEqual(self.mem.priority_for_zone('a.py'), 0.0)


# ---------------------------------------------------------------------------
# 7) summary() — texto para reportar
# ---------------------------------------------------------------------------
class TestSummary(unittest.TestCase):
    """summary() devuelve un string con los contadores clave."""

    def test_summary_returns_non_empty_string(self):
        mem = memory.Memory()
        s = mem.summary()
        self.assertIsInstance(s, str)
        self.assertGreater(len(s), 0)

    def test_summary_includes_header(self):
        mem = memory.Memory()
        self.assertIn('Memoria del harness', mem.summary())

    def test_summary_includes_all_counters(self):
        mem = memory.Memory()
        mem.total_rounds = 5
        mem.total_bugs_found = 3
        mem.total_fixes_applied = 2
        s = mem.summary()
        for needle in ('Rondas totales', 'Bugs encontrados',
                       'Falsos positivos', 'Fixes aplicados', 'Zonas tracked'):
            self.assertIn(needle, s, f'falta {needle!r} en summary')

    def test_summary_includes_counter_values(self):
        mem = memory.Memory()
        mem.total_rounds = 12
        mem.total_bugs_found = 7
        s = mem.summary()
        self.assertIn('12', s)
        self.assertIn('7', s)

    def test_summary_shows_nunca_when_never_ran(self):
        mem = memory.Memory()
        self.assertIn('nunca', mem.summary())

    def test_summary_includes_open_bugs_section(self):
        mem = memory.Memory()
        mem.record_bug('a.py', 1, 'foo bar baz qux')
        s = mem.summary()
        self.assertIn('Bugs abiertos', s)
        self.assertIn('a.py:1', s)

    def test_summary_omits_open_bugs_section_when_none(self):
        mem = memory.Memory()
        s = mem.summary()
        self.assertNotIn('Bugs abiertos', s)

    def test_summary_omits_open_bugs_when_all_fixed(self):
        mem = memory.Memory()
        mem.record_bug('a.py', 1, 'foo bar', fix_commit='x1')
        self.assertNotIn('Bugs abiertos', mem.summary())


# ---------------------------------------------------------------------------
# 8) Concurrencia: record_bug no debe corromper el estado
# ---------------------------------------------------------------------------
class TestConcurrent(unittest.TestCase):
    """10 hilos llamando record_bug sobre el mismo hash deben terminar sin
    excepciones y dejar un único BugRecord en el dict."""

    def test_concurrent_record_bug_does_not_corrupt_state(self):
        mem = memory.Memory()
        n = 10
        barrier = threading.Barrier(n)
        errors = []

        def worker():
            try:
                barrier.wait()
                mem.record_bug('a.py', 1, 'foo bar')
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f'errores en workers: {errors}')
        rec = mem.get_bug('a.py', 1, 'foo bar')
        self.assertIsNotNone(rec)
        # Mismo hash → un solo BugRecord en el dict.
        self.assertEqual(len(mem.bugs), 1)
        # total_bugs_found cuenta altas, no recurrencias → debe ser 1.
        self.assertEqual(mem.total_bugs_found, 1)
        # times_confirmed debería ser ≥ 1 (al menos el alta inicial).
        # Sin lock, valores entre 1 y n son posibles; verificamos cota inferior
        # y superior para detectar corrupción (e.g. None, negativo, > n).
        self.assertGreaterEqual(rec.times_confirmed, 1)
        self.assertLessEqual(rec.times_confirmed, n)

    def test_concurrent_record_bug_distinct_hashes_all_tracked(self):
        """Hilos con hashes distintos no deben perder altas (cada uno es único)."""
        mem = memory.Memory()
        n = 10
        barrier = threading.Barrier(n)
        errors = []

        def worker(i):
            try:
                barrier.wait()
                mem.record_bug(f'f{i}.py', 1, f'snippet number {i}')
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f'errores en workers: {errors}')
        self.assertEqual(len(mem.bugs), n)
        self.assertEqual(mem.total_bugs_found, n)


if __name__ == '__main__':
    unittest.main(verbosity=2)
