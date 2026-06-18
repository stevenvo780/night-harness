#!/usr/bin/env python3
"""Edge cases del flujo de fixes: _is_risky edge cases, _add_to_review edge cases,
discover_zones con directorios vacíos, _apply_and_validate con write failures,
fixer_worker con file-not-found / all-fixers-fail / multi-spec chain / dry-run
consolidado, _commit_fix con paths con caracteres especiales.

Correr:  python3 -m unittest tests.test_fixer_flow_edge -v
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402
import tiers  # noqa: E402


def _make_suspect(file='app/(panel)/x/page.tsx', line=10,
                  snippet='const x = 1;', why='algo', severity='medium'):
    return harness.Suspect(file=file, line=line, snippet=snippet, why=why, severity=severity)


def _make_bug(suspect=None):
    return harness.Bug(suspect=suspect or _make_suspect(),
                       votes_real=2, votes_not_bug=0)


def _make_fixer_spec(model_id='codex:gpt-5.5'):
    return tiers.ModelSpec(
        id=model_id, family='codex', role='cloud_mid', relative_cost=4,
    )


# ---------------------------------------------------------------------------
# _is_risky: edge cases
# ---------------------------------------------------------------------------

class TestIsRiskyEdge(unittest.TestCase):
    """Casos borde: empty suspect, substring en palabras no relacionadas, etc."""

    def test_empty_suspect_returns_false(self):
        s = harness.Suspect(file='', line=0, snippet='', why='')
        self.assertFalse(harness._is_risky(s))

    def test_substring_inside_unrelated_word(self):
        # 'admin' está en RISKY_KEYWORDS y matchea con substring (no word-boundary).
        # 'administrator' contiene 'admin' → debe dar True.
        s = _make_suspect(snippet='check administrator role')
        self.assertTrue(harness._is_risky(s))

    def test_in_stock_unrelated_word(self):
        # 'stock' está en la lista; también matchea con 'outstocked' o 'stockage'.
        s = _make_suspect(why='stockage de objetos incorrecto')
        self.assertTrue(harness._is_risky(s))

    def test_all_fields_combined(self):
        # Si CUALQUIER campo contiene un keyword, devuelve True.
        s = _make_suspect(
            file='lib/util.ts',
            line=1,
            snippet='// harmless',
            why='involucra manejo de orden y entrega',
        )
        self.assertTrue(harness._is_risky(s))

    def test_safe_words_return_false(self):
        s = _make_suspect(
            file='lib/reportes/util.ts',
            line=100,
            snippet='const x = await fetch(...)',
            why='tabla no se refresca',
        )
        self.assertFalse(harness._is_risky(s))

    def test_special_chars_in_text(self):
        # Los caracteres especiales no deben romper el match.
        s = _make_suspect(snippet='admin@empresa!')
        self.assertTrue(harness._is_risky(s))

    def test_unicode_text_safe(self):
        # Texto con caracteres Unicode pero sin keywords → False.
        s = _make_suspect(snippet='const ñoño = "cliente";')
        self.assertFalse(harness._is_risky(s))


# ---------------------------------------------------------------------------
# _add_to_review: edge cases
# ---------------------------------------------------------------------------

class TestAddToReviewEdge(unittest.TestCase):
    """Persistencia y forma de los items en REVIEW_FILE."""

    def setUp(self):
        self._orig = harness.REVIEW_FILE
        self.tmpdir = tempfile.mkdtemp(prefix='review-edge-')
        harness.REVIEW_FILE = Path(self.tmpdir) / 'review.json'

    def tearDown(self):
        harness.REVIEW_FILE = self._orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_appends_in_order(self):
        for i in range(5):
            harness._add_to_review(
                _make_suspect(file=f'f{i}.ts', line=i),
                f'commit{i}', 'codex:gpt-5.5',
            )
        items = json.loads(harness.REVIEW_FILE.read_text())
        self.assertEqual(len(items), 5)
        for i, item in enumerate(items):
            self.assertEqual(item['commit'], f'commit{i}')
            self.assertEqual(item['file'], f'f{i}.ts')

    def test_long_snippet_truncated(self):
        long_sn = 'X' * 1000
        harness._add_to_review(_make_suspect(snippet=long_sn), 'c', 'm')
        items = json.loads(harness.REVIEW_FILE.read_text())
        self.assertEqual(len(items[0]['snippet']), 200)

    def test_short_snippet_preserved_verbatim(self):
        short_sn = 'const x = 1;'
        harness._add_to_review(_make_suspect(snippet=short_sn), 'c', 'm')
        items = json.loads(harness.REVIEW_FILE.read_text())
        self.assertEqual(items[0]['snippet'], short_sn)

    def test_empty_existing_file_overwrites(self):
        # REVIEW_FILE existe pero vacío.
        harness.REVIEW_FILE.write_text('')
        harness._add_to_review(_make_suspect(), 'c', 'm')
        items = json.loads(harness.REVIEW_FILE.read_text())
        self.assertEqual(len(items), 1)

    def test_array_with_garbage_overwrites(self):
        # REVIEW_FILE contiene un array con items que no son dicts.
        harness.REVIEW_FILE.write_text('[1, 2, 3]')
        harness._add_to_review(_make_suspect(), 'c', 'm')
        # json.loads('[1,2,3]') → [1,2,3]; .append(dict) → [1,2,3,{...}].
        items = json.loads(harness.REVIEW_FILE.read_text())
        self.assertEqual(len(items), 4)
        self.assertEqual(items[3]['commit'], 'c')


# ---------------------------------------------------------------------------
# discover_zones: edge cases
# ---------------------------------------------------------------------------

class TestDiscoverZonesEdge(unittest.TestCase):
    """Casos límite: panel vacío, archivos sueltos, zonas con caracteres."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='zones-edge-')
        self._orig_wt = harness.WT
        self._orig_subdir = harness.SUBDIR
        self._orig_wtsub = harness.WT_SUBDIR
        harness.WT = Path(self.tmpdir)
        harness.SUBDIR = 'apps/dashboard'
        harness.WT_SUBDIR = harness.WT / harness.SUBDIR
        (harness.WT / harness.SUBDIR).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        harness.WT = self._orig_wt
        harness.SUBDIR = self._orig_subdir
        harness.WT_SUBDIR = self._orig_wtsub
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_panel_dir_returns_empty(self):
        panel = harness.WT / harness.SUBDIR / 'app' / '(panel)'
        panel.mkdir(parents=True)
        self.assertEqual(harness.discover_zones(), [])

    def test_panel_dir_is_a_file_returns_empty(self):
        # Si (panel) es un archivo (no directorio), iterdir() falla.
        # El código usa `entry.is_dir()` → ese caso cae. Si panel no es dir,
        # discover_zones chequea panel.exists() y devuelve []. Si panel es
        # un archivo, .exists() es True y entra a iterdir. En ese caso
        # no debería crashear.
        panel = harness.WT / harness.SUBDIR / 'app' / '(panel)'
        panel.parent.mkdir(parents=True, exist_ok=True)
        panel.write_text('not a dir')
        try:
            zones = harness.discover_zones()
            # Si no crashea, debe devolver [] (un archivo no es .is_dir()).
            self.assertEqual(zones, [])
        except (NotADirectoryError, OSError):
            # Si el OS rechaza iterdir() sobre un archivo, también es aceptable.
            pass

    def test_hidden_dirs_actually_included(self):
        # El código SOLO excluye grupos (entries con '(' al inicio). NO excluye
        # dirs con '.' (e.g. '.git', '.next'). Documentamos el comportamiento real
        # para que un cambio futuro al filtro sea visible en este test.
        panel = harness.WT / harness.SUBDIR / 'app' / '(panel)'
        panel.mkdir(parents=True)
        (panel / '.hidden').mkdir()
        (panel / '.git').mkdir()
        (panel / 'visible').mkdir()
        zones = harness.discover_zones()
        # Solo paren groups se filtran; hidden dirs pasan tal cual.
        self.assertIn('app/(panel)/visible/', zones)
        self.assertNotIn('app/(panel)/(auth)/', zones)
        # (Opcional) aceptamos que .hidden y .git SÍ estén: depende del caller.
        self.assertEqual(len(zones), 3)

    def test_substring_dirs_under_panel_excluded(self):
        panel = harness.WT / harness.SUBDIR / 'app' / '(panel)'
        panel.mkdir(parents=True)
        # Subdirs anidados no son zonas (solo el primer nivel cuenta).
        (panel / 'a').mkdir()
        (panel / 'a' / 'b').mkdir()
        zones = harness.discover_zones()
        self.assertEqual(zones, ['app/(panel)/a/'])


# ---------------------------------------------------------------------------
# _apply_and_validate: edge cases
# ---------------------------------------------------------------------------

class TestApplyAndValidateEdge(unittest.TestCase):
    """Bordes: write failures, patches que sí tocan suspect.file, full file."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='av-edge-')
        self._orig_wt = harness.WT
        self._orig_subdir = harness.SUBDIR
        self._orig_wtsub = harness.WT_SUBDIR
        harness.WT = Path(self.tmpdir)
        harness.SUBDIR = 'apps/dashboard'
        harness.WT_SUBDIR = harness.WT / harness.SUBDIR
        (harness.WT / harness.SUBDIR).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        harness.WT = self._orig_wt
        harness.SUBDIR = self._orig_subdir
        harness.WT_SUBDIR = self._orig_wtsub
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_full_file_content_is_written_verbatim(self):
        rel = 'app/(panel)/x/page.tsx'
        path = harness.WT / harness.SUBDIR / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('OLD\n')
        new = 'NEW\nwith special chars: ñ, ü, €, "quotes"'
        suspect = _make_suspect(file=rel, line=1)
        with patch('harness._run_tsc', return_value=True), \
             patch('harness._run_smoke_subset', return_value=True):
            ok = harness._apply_and_validate(path, new, suspect)
        self.assertTrue(ok)
        self.assertEqual(path.read_text(), new)

    def test_full_file_strips_markdown_fences_via_caller(self):
        # _apply_and_validate NO quita fences — eso lo hace fixer_worker.
        rel = 'app/(panel)/x/page.tsx'
        path = harness.WT / harness.SUBDIR / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('old\n')
        suspect = _make_suspect(file=rel, line=1)
        with patch('harness._run_tsc', return_value=True), \
             patch('harness._run_smoke_subset', return_value=True):
            ok = harness._apply_and_validate(path, '```typescript\nnew\n```', suspect)
        self.assertTrue(ok)
        # El contenido escrito incluye los fences (el caller es quien los quita).
        self.assertEqual(path.read_text(), '```typescript\nnew\n```')

    def test_patch_with_suspect_file_is_accepted(self):
        rel = 'app/(panel)/x/page.tsx'
        path = harness.WT / harness.SUBDIR / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('const x = 1;\n')
        suspect = _make_suspect(file=rel, line=1)
        diff = (
            f'--- a/{rel}\n+++ b/{rel}\n@@ -1 +1 @@\n-const x = 1;\n+const x = 2;\n'
        )
        with patch('harness._apply_patch', return_value=True), \
             patch('harness._revert_patch') as mock_revert, \
             patch('harness._run_tsc', return_value=True), \
             patch('harness._run_smoke_subset', return_value=True):
            ok = harness._apply_and_validate(path, diff, suspect)
        self.assertTrue(ok)
        # El patch toca suspect.file → no se llama _revert_patch.
        mock_revert.assert_not_called()

    def test_patch_path_with_subdir_prefix_is_accepted(self):
        # Si SUBDIR está seteado, el diff puede usar '<subdir>/<file>'.
        rel = 'app/(panel)/x/page.tsx'
        path = harness.WT / harness.SUBDIR / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('const x = 1;\n')
        suspect = _make_suspect(file=rel, line=1)
        prefixed = f'{harness.SUBDIR}/{rel}'
        diff = (
            f'--- a/{prefixed}\n+++ b/{prefixed}\n@@ -1 +1 @@\n-const x = 1;\n+const x = 2;\n'
        )
        with patch('harness._apply_patch', return_value=True), \
             patch('harness._run_tsc', return_value=True), \
             patch('harness._run_smoke_subset', return_value=True):
            ok = harness._apply_and_validate(path, diff, suspect)
        self.assertTrue(ok)

    def test_write_failure_returns_false(self):
        rel = 'app/(panel)/x/page.tsx'
        path = harness.WT / harness.SUBDIR / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        suspect = _make_suspect(file=rel, line=1)
        with patch('pathlib.Path.write_text', side_effect=OSError('read-only fs')):
            ok = harness._apply_and_validate(path, 'new', suspect)
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# _commit_fix: edge cases
# ---------------------------------------------------------------------------

class TestCommitFixEdge(unittest.TestCase):
    """_commit_fix con paths de distintos formatos, fallos de subprocess."""

    def test_handles_path_with_spaces(self):
        # El path puede tener espacios (e.g. 'app/(panel)/mis rutas/page.tsx').
        path = Path('/tmp/demeter-loop/apps/dashboard/app/(panel)/mis rutas/page.tsx')
        suspect = _make_suspect(file='app/(panel)/mis rutas/page.tsx', line=1)
        with patch('harness.subprocess.run') as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stderr=''),
                MagicMock(stdout='feedbeef0001\n'),
            ]
            sha = harness._commit_fix(path, suspect)
        self.assertEqual(sha, 'feedbeef0001')
        # 1ª call: git add <rel_path>
        add_args = mock_run.call_args_list[0][0][0]
        self.assertIn('git', add_args)
        self.assertIn('add', add_args)

    def test_revparse_failure_returns_empty(self):
        # Si rev-parse crashea, git add+commit ya pasaron pero no tenemos SHA.
        # El except captura CalledProcessError; rev-parse no lo eleva aquí.
        path = Path('/tmp/demeter-loop/apps/dashboard/app/(panel)/x/page.tsx')
        suspect = _make_suspect(file='app/(panel)/x/page.tsx', line=1)
        with patch('harness.subprocess.run') as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stderr=''),
                MagicMock(stdout=''),  # rev-parse vacío
            ]
            sha = harness._commit_fix(path, suspect)
        self.assertEqual(sha, '')

    def test_commit_message_includes_suspect_metadata(self):
        path = Path('/tmp/demeter-loop/apps/dashboard/app/(panel)/x/page.tsx')
        suspect = _make_suspect(
            file='app/(panel)/x/page.tsx',
            line=42,
            why='falta validación de tenantId en la query',
        )
        with patch('harness.subprocess.run') as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stderr=''),
                MagicMock(stdout='abc1234\n'),
            ]
            harness._commit_fix(path, suspect)
        # El segundo call es git commit -m <msg>.
        commit_call = mock_run.call_args_list[1]
        commit_args = commit_call[0][0]
        self.assertEqual(commit_args[0], 'git')
        self.assertEqual(commit_args[1], 'commit')
        # El mensaje se pasa en -m.
        msg_idx = commit_args.index('-m') + 1
        msg = commit_args[msg_idx]
        self.assertIn('app/(panel)/x/page.tsx:42', msg)
        self.assertIn('falta validación', msg)
        self.assertIn(suspect.hash, msg)


# ---------------------------------------------------------------------------
# _file_lock: edge cases
# ---------------------------------------------------------------------------

class TestFileLockEdge(unittest.TestCase):
    """_file_lock con muchos archivos, semántica de adquisición."""

    def setUp(self):
        self._orig = dict(harness._FILE_LOCKS)
        harness._FILE_LOCKS.clear()

    def tearDown(self):
        harness._FILE_LOCKS.clear()
        harness._FILE_LOCKS.update(self._orig)

    def test_different_files_different_locks(self):
        a = harness._file_lock('a.ts')
        b = harness._file_lock('b.ts')
        c = harness._file_lock('c.tsx')
        self.assertIsNot(a, b)
        self.assertIsNot(b, c)
        self.assertIsNot(a, c)

    def test_same_file_same_lock_object(self):
        # Caché real: dos llamadas devuelven el MISMO objeto (no uno nuevo).
        x1 = harness._file_lock('zz.ts')
        x2 = harness._file_lock('zz.ts')
        x3 = harness._file_lock('zz.ts')
        self.assertIs(x1, x2)
        self.assertIs(x2, x3)
        self.assertEqual(len(harness._FILE_LOCKS), 1)

    def test_lock_is_acquirable_and_releasable(self):
        lock = harness._file_lock('acquirable.ts')
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()
        # Re-adquirible.
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_lock_blocks_second_acquire(self):
        lock = harness._file_lock('blocking.ts')
        lock.acquire()
        try:
            self.assertFalse(lock.acquire(blocking=False))
        finally:
            lock.release()
        # Tras release, vuelve a ser adquirible.
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()


# ---------------------------------------------------------------------------
# fixer_worker: edge cases
# ---------------------------------------------------------------------------

class TestFixerWorkerEdge(unittest.TestCase):
    """Casos borde: file not found, all-fail, multi-spec chain, dry-run, etc."""

    def setUp(self):
        if not hasattr(harness, 'args') or not isinstance(harness.args, argparse.Namespace):
            harness.args = argparse.Namespace(dry_run=False, no_e2e=False)
        harness.args.dry_run = False
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()
        self._orig_locks = dict(harness._FILE_LOCKS)
        harness._FILE_LOCKS.clear()
        self.tmpdir = tempfile.mkdtemp(prefix='fixer-worker-edge-')
        self._orig_wt = harness.WT
        self._orig_subdir = harness.SUBDIR
        self._orig_wtsub = harness.WT_SUBDIR
        self._orig_fleet = harness.FLEET
        harness.WT = Path(self.tmpdir)
        harness.SUBDIR = 'apps/dashboard'
        harness.WT_SUBDIR = harness.WT / harness.SUBDIR
        (harness.WT / harness.SUBDIR).mkdir(parents=True, exist_ok=True)
        self.target_rel = 'app/(panel)/x/page.tsx'
        self.target = harness.WT / harness.SUBDIR / self.target_rel
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_text('const x = 1;\n')
        self._fixes_attempted = harness.STATS['fixes_attempted']
        self._fixes_succeeded = harness.STATS['fixes_succeeded']

    def tearDown(self):
        harness._FILE_LOCKS.clear()
        harness._FILE_LOCKS.update(self._orig_locks)
        harness.WT = self._orig_wt
        harness.SUBDIR = self._orig_subdir
        harness.WT_SUBDIR = self._orig_wtsub
        harness.FLEET = self._orig_fleet
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_file_not_found_returns_early(self):
        # Suspect apunta a un archivo que no existe en el worktree.
        bug = _make_bug(
            suspect=_make_suspect(file='app/(panel)/nope/page.tsx', line=1),
        )
        with patch.object(harness.MEM, 'get_bug', return_value=None), \
             patch('harness.quota.filter_tier') as mock_qt, \
             patch('harness.FLEET', None), \
             patch('harness.call_model') as mock_call:
            harness.fixer_worker(bug)
        # Se llegó a filter_tier, pero el archivo no existe → return sin
        # invocar call_model.
        mock_qt.assert_called_once()
        mock_call.assert_not_called()
        # fix_attempted SÍ se incrementa (la lógica lo hace ANTES del file check).
        self.assertEqual(harness.STATS['fixes_attempted'], self._fixes_attempted + 1)
        # Pero fix_succeeded NO.
        self.assertEqual(harness.STATS['fixes_succeeded'], self._fixes_succeeded)

    def test_all_fixers_fail_marks_status_done(self):
        bug = _make_bug()
        spec = _make_fixer_spec()
        with patch.object(harness.MEM, 'get_bug', return_value=None), \
             patch('harness.quota.filter_tier', return_value=[spec]), \
             patch('harness.FLEET', None), \
             patch('harness.call_model', return_value='const x = 1;'), \
             patch('harness._apply_and_validate', return_value=False):
            harness.fixer_worker(bug)
        # fixes_attempted subió 1, fixes_succeeded NO.
        self.assertEqual(harness.STATS['fixes_attempted'], self._fixes_attempted + 1)
        self.assertEqual(harness.STATS['fixes_succeeded'], self._fixes_succeeded)
        # El thread quedó 'done' con task 'todos fallaron'.
        key = f'fixer-{bug.suspect.file}'
        with harness.THREAD_STATES_LOCK:
            st = harness.THREAD_STATES.get(key, {})
        self.assertEqual(st.get('status'), 'done')
        self.assertEqual(st.get('current_task'), 'todos fallaron')

    def test_chains_multiple_specs_until_success(self):
        bug = _make_bug()
        specs = [_make_fixer_spec(m) for m in (
            'codex:gpt-5.5', 'minimax/MiniMax-M3', 'gemini:gemini-2.5-pro',
        )]
        with patch.object(harness.MEM, 'get_bug', return_value=None), \
             patch('harness.quota.filter_tier', return_value=specs), \
             patch('harness.FLEET', None), \
             patch('harness.call_model', side_effect=[
                 # 1: skip (== original)
                 'const x = 1;',
                 # 2: cambia pero validación falla
                 'try { new_fix }',
                 # 3: cambia y validación pasa
                 'final fix',
             ]), \
             patch('harness._apply_and_validate', side_effect=[False, True]), \
             patch('harness._commit_fix', return_value='chainSHA123') as mock_commit:
            harness.fixer_worker(bug)
        # 1 commit, 1 succeeded, 3 specs intentados.
        self.assertEqual(mock_commit.call_count, 1)
        self.assertEqual(harness.STATS['fixes_succeeded'], self._fixes_succeeded + 1)

    def test_fleet_fixer_deduped_from_tier3(self):
        # El fleet local debe ir primero, y dedupe lo deja UNA sola vez.
        # Hacemos que el local FALLE (devuelve == original) para que el chain
        # avance a codex.
        bug = _make_bug()
        local = _make_fixer_spec('ollama:qwen3-coder:30b')
        codex = _make_fixer_spec('codex:gpt-5.5')
        mock_fleet = MagicMock()
        mock_fleet.fixer_spec.return_value = local
        ids_called = []
        def fake_call_model(spec, *args, **kw):
            ids_called.append(spec.id)
            if spec.id == 'ollama:qwen3-coder:30b':
                return 'const x = 1;'  # == original → skip
            return 'real change from codex'
        with patch.object(harness.MEM, 'get_bug', return_value=None), \
             patch('harness.quota.filter_tier', return_value=[codex, local]), \
             patch('harness.FLEET', mock_fleet), \
             patch('harness.call_model', side_effect=fake_call_model), \
             patch('harness._apply_and_validate', return_value=True), \
             patch('harness._commit_fix', return_value='shaX'):
            harness.fixer_worker(bug)
        # El local se llama primero; al fallar, codex se llama segundo.
        self.assertEqual(ids_called[0], 'ollama:qwen3-coder:30b')
        self.assertIn('codex:gpt-5.5', ids_called)
        # Y el local NO se duplica (solo 1 occurrence, no 2).
        self.assertEqual(ids_called.count('ollama:qwen3-coder:30b'), 1)

    def test_dry_run_does_not_run_gates(self):
        bug = _make_bug()
        spec = _make_fixer_spec()
        harness.args.dry_run = True
        with patch.object(harness.MEM, 'get_bug', return_value=None), \
             patch.object(harness.MEM, 'record_bug') as mock_rec, \
             patch('harness.quota.filter_tier', return_value=[spec]), \
             patch('harness.FLEET', None), \
             patch('harness.call_model', return_value='dry content\n'), \
             patch('harness._apply_and_validate') as mock_av, \
             patch('harness._commit_fix') as mock_commit, \
             patch('harness._run_tsc') as mock_tsc:
            harness.fixer_worker(bug)
        # Ningún gate real se ejecutó.
        mock_av.assert_not_called()
        mock_commit.assert_not_called()
        mock_tsc.assert_not_called()
        # Se grabó el bug con 'DRY-RUN' y se subió fixes_succeeded.
        kwargs = mock_rec.call_args.kwargs
        self.assertEqual(kwargs['fix_commit'], 'DRY-RUN')
        self.assertEqual(harness.STATS['fixes_succeeded'], self._fixes_succeeded + 1)

    def test_dedup_uses_get_bug_with_correct_args(self):
        bug = _make_bug()
        rec = MagicMock()
        rec.fixed = True
        with patch.object(harness.MEM, 'get_bug', return_value=rec) as mock_get:
            harness.fixer_worker(bug)
        # get_bug(file, line, snippet) — los 3 posicionales.
        args = mock_get.call_args[0]
        self.assertEqual(args[0], bug.suspect.file)
        self.assertEqual(args[1], bug.suspect.line)
        self.assertEqual(args[2], bug.suspect.snippet)

    def test_fleet_fixer_dedup_keeps_first_occurrence(self):
        # El local aparece varias veces en tier3; el dedup deja UNA copia al frente.
        bug = _make_bug()
        local = _make_fixer_spec('ollama:qwen3-coder:30b')
        mock_fleet = MagicMock()
        mock_fleet.fixer_spec.return_value = local
        ids_called = []
        def fake(spec, *a, **kw):
            ids_called.append(spec.id)
            return 'changed'
        with patch.object(harness.MEM, 'get_bug', return_value=None), \
             patch('harness.quota.filter_tier', return_value=[local]), \
             patch('harness.FLEET', mock_fleet), \
             patch('harness.call_model', side_effect=fake), \
             patch('harness._apply_and_validate', return_value=True), \
             patch('harness._commit_fix', return_value='sha'):
            harness.fixer_worker(bug)
        # Solo UNA llamada (no se duplica).
        self.assertEqual(ids_called.count('ollama:qwen3-coder:30b'), 1)

    def test_lock_released_after_fixer_returns(self):
        bug = _make_bug()
        spec = _make_fixer_spec()
        with patch.object(harness.MEM, 'get_bug', return_value=None), \
             patch('harness.quota.filter_tier', return_value=[spec]), \
             patch('harness.FLEET', None), \
             patch('harness.call_model', return_value='fixed'), \
             patch('harness._apply_and_validate', return_value=True), \
             patch('harness._commit_fix', return_value='abc'):
            harness.fixer_worker(bug)
        # El lock del archivo debe estar liberado (no adquirido por nadie).
        lock = harness._file_lock(bug.suspect.file)
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_lock_released_on_exception(self):
        # Si _apply_and_validate lanza, el lock debe liberarse en el `finally`.
        bug = _make_bug()
        spec = _make_fixer_spec()
        with patch.object(harness.MEM, 'get_bug', return_value=None), \
             patch('harness.quota.filter_tier', return_value=[spec]), \
             patch('harness.FLEET', None), \
             patch('harness.call_model', return_value='fixed'), \
             patch('harness._apply_and_validate', side_effect=RuntimeError('boom')), \
             patch('harness._commit_fix'):
            with self.assertRaises(RuntimeError):
                harness.fixer_worker(bug)
        # Lock liberado → adquirible.
        lock = harness._file_lock(bug.suspect.file)
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()


if __name__ == '__main__':
    unittest.main(verbosity=2)
