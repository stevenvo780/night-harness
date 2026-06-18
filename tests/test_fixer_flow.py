#!/usr/bin/env python3
"""Tests del flujo de fixes: _is_risky, _add_to_review, _file_lock, discover_zones,
discover_code_zones, _commit_fix (mocked), _apply_and_validate (mocked) y fixer_worker
(major branches).

Cubre:
  · _is_risky: cada keyword de RISKY_KEYWORDS + case-insensitive + clean
  · _add_to_review: crea archivo, appendea, maneja JSON corrupto
  · _file_lock: cachea el Lock por archivo
  · discover_zones: zonas de app/(panel)/<name>/, excluye (groups) y hidden
  · discover_code_zones: agrupa .ts/.tsx excluyendo .test.ts, trunca a 30
  · _commit_fix: SHA en éxito, None en fallo
  · _apply_and_validate: full file vs patch, gates tsc+E2E, multifile
  · fixer_worker: dedup, lock contention, no fixers, chain, fleet prepend,
    risky → review, revert, dry-run, status='done'

Correr:  python3 -m unittest tests.test_fixer_flow -v
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_suspect(file='app/(panel)/clientes/page.tsx', line=42,
                  snippet='const x = await db.query(...)', why='falta tenantId',
                  severity='high'):
    return harness.Suspect(file=file, line=line, snippet=snippet, why=why, severity=severity)


def _make_bug(suspect=None, votes_real=2, votes_not_bug=0):
    return harness.Bug(suspect=suspect or _make_suspect(),
                       votes_real=votes_real, votes_not_bug=votes_not_bug)


def _make_fixer_spec(model_id='codex:gpt-5.5'):
    """Devuelve un ModelSpec para usar como fixer en los tests."""
    return tiers.ModelSpec(
        id=model_id, family='codex', role='cloud_mid', relative_cost=4,
    )


# ---------------------------------------------------------------------------
# 1) _is_risky: cubre cada keyword
# ---------------------------------------------------------------------------

class TestIsRisky(unittest.TestCase):
    """_is_risky devuelve True si `file + why + snippet` contiene un RISKY_KEYWORD."""

    def test_dinero_in_snippet(self):
        s = _make_suspect(snippet='// flujo de dinero en caja')
        self.assertTrue(harness._is_risky(s))

    def test_pago_in_why(self):
        s = _make_suspect(why='no valida el pago antes de marcar la orden')
        self.assertTrue(harness._is_risky(s))

    def test_cartera_in_file(self):
        s = _make_suspect(file='app/(panel)/cartera/dashboard.tsx')
        self.assertTrue(harness._is_risky(s))

    def test_cobro_in_why(self):
        s = _make_suspect(why='cobro duplicado en retry')
        self.assertTrue(harness._is_risky(s))

    def test_factura_in_snippet(self):
        s = _make_suspect(snippet='emitir factura sin tax_id')
        self.assertTrue(harness._is_risky(s))

    def test_stock_keyword(self):
        s = _make_suspect(why='no decrementa stock al confirmar venta')
        self.assertTrue(harness._is_risky(s))

    def test_inventario_keyword(self):
        s = _make_suspect(why='inventario negativo tras rollback')
        self.assertTrue(harness._is_risky(s))

    def test_despacho_keyword(self):
        s = _make_suspect(file='app/(panel)/despacho/page.tsx')
        self.assertTrue(harness._is_risky(s))

    def test_entrega_keyword(self):
        s = _make_suspect(why='entrega se duplica al reintentar')
        self.assertTrue(harness._is_risky(s))

    def test_ruta_keyword(self):
        s = _make_suspect(why='ruta de entrega no se persiste')
        self.assertTrue(harness._is_risky(s))

    def test_auth_keyword(self):
        s = _make_suspect(why='auth check skipped en middleware')
        self.assertTrue(harness._is_risky(s))

    def test_login_keyword(self):
        s = _make_suspect(snippet='async function login()')
        self.assertTrue(harness._is_risky(s))

    def test_signin_keyword(self):
        s = _make_suspect(why='signin no limpia la sesión anterior')
        self.assertTrue(harness._is_risky(s))

    def test_permiso_keyword(self):
        s = _make_suspect(why='permiso de admin no se chequea')
        self.assertTrue(harness._is_risky(s))

    def test_admin_keyword(self):
        s = _make_suspect(file='app/(panel)/admin/usuarios/page.tsx')
        self.assertTrue(harness._is_risky(s))

    def test_estado_keyword(self):
        s = _make_suspect(why='transicion de estado no es atómica')
        self.assertTrue(harness._is_risky(s))

    def test_transicion_keyword(self):
        s = _make_suspect(why='transicion sin validar source state')
        self.assertTrue(harness._is_risky(s))

    def test_orden_keyword(self):
        s = _make_suspect(snippet='const orden = await db.orders.find(...)')
        self.assertTrue(harness._is_risky(s))

    def test_pedido_keyword(self):
        # "pedido" coincide con 'orden' solo si está literalmente; pero también
        # queremos confirmar que un cambio de estado en pedidos dispara risky.
        s = _make_suspect(why='cambio de estado en pedido sin transacción')
        self.assertTrue(harness._is_risky(s))

    def test_clean_text_returns_false(self):
        s = _make_suspect(
            file='app/(panel)/reportes/page.tsx',
            line=10,
            snippet='const headers = ["nombre", "email", "fecha"]',
            why='tabla no se refresca tras filtro',
        )
        self.assertFalse(harness._is_risky(s))

    def test_is_case_insensitive(self):
        s = _make_suspect(snippet='ADMIN role check missing')
        self.assertTrue(harness._is_risky(s))
        s2 = _make_suspect(why='Login flow broken')
        self.assertTrue(harness._is_risky(s2))


# ---------------------------------------------------------------------------
# 2) _add_to_review
# ---------------------------------------------------------------------------

class TestAddToReview(unittest.TestCase):
    """_add_to_review appends an item to REVIEW_FILE."""

    def setUp(self):
        self._orig = harness.REVIEW_FILE
        self.tmpdir = tempfile.mkdtemp(prefix='fixer-review-')
        harness.REVIEW_FILE = Path(self.tmpdir) / 'review.json'

    def tearDown(self):
        harness.REVIEW_FILE = self._orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_creates_file_with_item(self):
        s = _make_suspect(file='app/(panel)/caja/page.tsx', line=99,
                          why='manejo de dinero incorrecto', snippet='const total = 0')
        harness._add_to_review(s, 'abc1234567', 'codex:gpt-5.5')
        self.assertTrue(harness.REVIEW_FILE.exists())
        items = json.loads(harness.REVIEW_FILE.read_text())
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item['commit'], 'abc1234567')
        self.assertEqual(item['file'], 'app/(panel)/caja/page.tsx')
        self.assertEqual(item['line'], 99)
        self.assertEqual(item['why'], 'manejo de dinero incorrecto')
        self.assertEqual(item['model'], 'codex:gpt-5.5')
        self.assertIn('created_at', item)
        self.assertIn('reason', item)
        self.assertIn('revisi', item['reason'].lower())

    def test_appends_to_existing_file(self):
        s1 = _make_suspect(file='a.ts', line=1)
        s2 = _make_suspect(file='b.ts', line=2, snippet='x = 1')
        harness._add_to_review(s1, 'commit1', 'codex:gpt-5.5')
        harness._add_to_review(s2, 'commit2', 'minimax/MiniMax-M3')
        items = json.loads(harness.REVIEW_FILE.read_text())
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['commit'], 'commit1')
        self.assertEqual(items[1]['commit'], 'commit2')
        self.assertEqual(items[1]['file'], 'b.ts')
        self.assertEqual(items[1]['model'], 'minimax/MiniMax-M3')

    def test_handles_corrupted_json(self):
        harness.REVIEW_FILE.write_text('{not valid json at all')
        s = _make_suspect(file='c.ts', line=3)
        harness._add_to_review(s, 'commitC', 'codex:gpt-5.5')
        items = json.loads(harness.REVIEW_FILE.read_text())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['commit'], 'commitC')
        self.assertEqual(items[0]['file'], 'c.ts')

    def test_snippet_truncated_to_200_chars(self):
        long_snippet = 'x' * 500
        s = _make_suspect(snippet=long_snippet)
        harness._add_to_review(s, 'commitX', 'codex:gpt-5.5')
        items = json.loads(harness.REVIEW_FILE.read_text())
        self.assertEqual(len(items[0]['snippet']), 200)


# ---------------------------------------------------------------------------
# 3) discover_zones
# ---------------------------------------------------------------------------

class TestDiscoverZones(unittest.TestCase):
    """discover_zones escanea app/(panel)/<name>/ excluyendo (groups) y hidden."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='fixer-zones-')
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

    def test_returns_sorted_zones(self):
        panel = harness.WT / harness.SUBDIR / 'app' / '(panel)'
        for name in ['zeta', 'alpha', 'mango', 'beta']:
            (panel / name).mkdir(parents=True)
        zones = harness.discover_zones()
        self.assertEqual(zones, [
            'app/(panel)/alpha/',
            'app/(panel)/beta/',
            'app/(panel)/mango/',
            'app/(panel)/zeta/',
        ])

    def test_excludes_paren_groups(self):
        panel = harness.WT / harness.SUBDIR / 'app' / '(panel)'
        (panel / 'visible').mkdir(parents=True)
        # Paren groups: same dir syntax used by Next.js — must be excluded.
        (panel / '(auth)').mkdir(parents=True)
        (panel / '(marketing)').mkdir(parents=True)
        zones = harness.discover_zones()
        self.assertEqual(zones, ['app/(panel)/visible/'])

    def test_returns_empty_if_panel_dir_missing(self):
        # No creamos app/(panel)/ → debe devolver lista vacía.
        zones = harness.discover_zones()
        self.assertEqual(zones, [])

    def test_excludes_files_only_includes_dirs(self):
        panel = harness.WT / harness.SUBDIR / 'app' / '(panel)'
        (panel / 'real_zone').mkdir(parents=True)
        # Crear un archivo suelto en el panel — no debe ser tratado como zona.
        (panel / 'stray_file.ts').write_text('// oops')
        zones = harness.discover_zones()
        self.assertEqual(zones, ['app/(panel)/real_zone/'])


# ---------------------------------------------------------------------------
# 4) discover_code_zones
# ---------------------------------------------------------------------------

class TestDiscoverCodeZones(unittest.TestCase):
    """discover_code_zones agrupa .ts/.tsx por zona y trunca a 30 archivos."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='fixer-code-zones-')
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

    def test_groups_ts_and_tsx(self):
        panel = harness.WT / harness.SUBDIR / 'app' / '(panel)' / 'clientes'
        panel.mkdir(parents=True)
        (panel / 'page.tsx').write_text('export default function(){}')
        (panel / 'components').mkdir()
        (panel / 'components' / 'Table.tsx').write_text('export default function(){}')
        (panel / 'helpers.ts').write_text('export const x = 1')
        # .test.ts NO debe incluirse.
        (panel / 'helpers.test.ts').write_text('test("x", () => {})')
        # Archivos fuera de la zona no entran.
        (harness.WT / harness.SUBDIR / 'app' / '(panel)' / 'otra').mkdir(parents=True)
        (harness.WT / harness.SUBDIR / 'app' / '(panel)' / 'otra' / 'page.tsx').write_text('')
        groups = harness.discover_code_zones()
        # Aplanamos para chequear contenido.
        flat = [f for g in groups for f in g]
        self.assertIn('app/(panel)/clientes/page.tsx', flat)
        self.assertIn('app/(panel)/clientes/components/Table.tsx', flat)
        self.assertIn('app/(panel)/clientes/helpers.ts', flat)
        self.assertNotIn('app/(panel)/clientes/helpers.test.ts', flat)

    def test_includes_lib_files_in_prefix_groups(self):
        lib = harness.WT / harness.SUBDIR / 'lib' / 'queries'
        lib.mkdir(parents=True)
        (lib / 'clientes.ts').write_text('export const x = 1')
        (lib / 'productos.ts').write_text('export const y = 2')
        # Test en lib también debe excluirse.
        (lib / 'clientes.test.ts').write_text('test("c", () => {})')
        groups = harness.discover_code_zones()
        flat = [f for g in groups for f in g]
        self.assertIn('lib/queries/clientes.ts', flat)
        self.assertIn('lib/queries/productos.ts', flat)
        self.assertNotIn('lib/queries/clientes.test.ts', flat)

    def test_truncates_to_30_files_per_zone(self):
        panel = harness.WT / harness.SUBDIR / 'app' / '(panel)' / 'big'
        panel.mkdir(parents=True)
        for i in range(40):
            (panel / f'f{i}.ts').write_text(f'export const x{i} = {i}')
        groups = harness.discover_code_zones()
        # Cada grupo es una lista; debe haber un grupo 'app/(panel)/big/' con 30.
        big_groups = [g for g in groups if any('big' in f for f in g)]
        self.assertEqual(len(big_groups), 1)
        self.assertEqual(len(big_groups[0]), 30)

    def test_excludes_test_files(self):
        panel = harness.WT / harness.SUBDIR / 'app' / '(panel)' / 'x'
        panel.mkdir(parents=True)
        (panel / 'a.ts').write_text('x')
        (panel / 'a.test.ts').write_text('test')
        (panel / 'helper.test.ts').write_text('test2')
        groups = harness.discover_code_zones()
        flat = [f for g in groups for f in g]
        self.assertIn('app/(panel)/x/a.ts', flat)
        # El filtro solo excluye '.test.ts' (no '.test.tsx' ni '.spec.ts').
        self.assertNotIn('app/(panel)/x/a.test.ts', flat)
        self.assertNotIn('app/(panel)/x/helper.test.ts', flat)


# ---------------------------------------------------------------------------
# 5) _file_lock
# ---------------------------------------------------------------------------

class TestFileLock(unittest.TestCase):
    """_file_lock devuelve threading.Lock cacheado por path."""

    def setUp(self):
        self._orig = dict(harness._FILE_LOCKS)
        harness._FILE_LOCKS.clear()

    def tearDown(self):
        harness._FILE_LOCKS.clear()
        harness._FILE_LOCKS.update(self._orig)

    def test_returns_lock_instance(self):
        lock = harness._file_lock('a.ts')
        self.assertIsInstance(lock, type(threading.Lock()))

    def test_returns_cached_lock(self):
        a1 = harness._file_lock('a.ts')
        a2 = harness._file_lock('a.ts')
        self.assertIs(a1, a2)
        # Distinto archivo → distinto lock.
        b1 = harness._file_lock('b.ts')
        self.assertIsNot(a1, b1)


# ---------------------------------------------------------------------------
# 6) _commit_fix (mocked subprocess)
# ---------------------------------------------------------------------------

class TestCommitFix(unittest.TestCase):
    """_commit_fix corre git add+commit+rev-parse; devuelve SHA o None."""

    def test_success_returns_sha(self):
        path = Path('/tmp/demeter-loop/apps/dashboard/app/(panel)/x/page.tsx')
        suspect = _make_suspect(file='app/(panel)/x/page.tsx', line=1)
        fake_rev = MagicMock()
        fake_rev.stdout = 'deadbeef1234567890abcdef\n'
        with patch('harness.subprocess.run') as mock_run:
            # 1ª call: git add, 2ª: git commit, 3ª: git rev-parse
            mock_run.side_effect = [
                MagicMock(returncode=0),   # add
                MagicMock(returncode=0, stderr=''),  # commit
                fake_rev,                  # rev-parse
            ]
            sha = harness._commit_fix(path, suspect)
        self.assertEqual(sha, 'deadbeef1234567890abcdef')
        self.assertEqual(mock_run.call_count, 3)
        # 1ª: git add
        add_args = mock_run.call_args_list[0][0][0]
        self.assertEqual(add_args[:2], ['git', 'add'])
        # 2ª: git commit
        commit_args = mock_run.call_args_list[1][0][0]
        self.assertEqual(commit_args[0], 'git')
        self.assertEqual(commit_args[1], 'commit')

    def test_git_fail_returns_none(self):
        import subprocess as sp
        path = Path('/tmp/demeter-loop/apps/dashboard/app/(panel)/x/page.tsx')
        suspect = _make_suspect(file='app/(panel)/x/page.tsx', line=1)
        with patch('harness.subprocess.run') as mock_run:
            mock_run.side_effect = sp.CalledProcessError(
                returncode=1, cmd=['git', 'commit'], stderr='nothing to commit',
            )
            sha = harness._commit_fix(path, suspect)
        self.assertIsNone(sha)


# ---------------------------------------------------------------------------
# 7) _apply_and_validate (mocked subprocess)
# ---------------------------------------------------------------------------

class TestApplyAndValidate(unittest.TestCase):
    """_apply_and_validate: full file vs patch, gates tsc+E2E."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='apply-validate-')
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

    def _write_target(self, rel_path='app/(panel)/x/page.tsx', content='orig\n'):
        full = harness.WT / harness.SUBDIR / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        return full

    def test_full_file_writes_and_validates(self):
        path = self._write_target()
        suspect = _make_suspect(file='app/(panel)/x/page.tsx', line=1)
        new = '// fixed\n'
        with patch('harness._run_tsc', return_value=True), \
             patch('harness._run_smoke_subset', return_value=True):
            ok = harness._apply_and_validate(path, new, suspect)
        self.assertTrue(ok)
        self.assertEqual(path.read_text(), '// fixed\n')

    def test_patch_applies_and_validates(self):
        path = self._write_target(content='const x = 1;\n')
        suspect = _make_suspect(file='app/(panel)/x/page.tsx', line=1)
        rel = str(path.relative_to(harness.WT))
        diff = (
            f'--- a/{rel}\n+++ b/{rel}\n@@ -1 +1 @@\n-const x = 1;\n+const x = 2;\n'
        )
        with patch('harness._apply_patch', return_value=True), \
             patch('harness._revert_patch', return_value=True), \
             patch('harness._run_tsc', return_value=True), \
             patch('harness._run_smoke_subset', return_value=True):
            ok = harness._apply_and_validate(path, diff, suspect)
        self.assertTrue(ok)

    def test_multifile_patch_rejected(self):
        path = self._write_target()
        suspect = _make_suspect(file='app/(panel)/x/page.tsx', line=1)
        diff = (
            '--- a/a.ts\n+++ b/a.ts\n@@ -1 +1 @@\n-x\n+y\n'
            '--- a/b.ts\n+++ b/b.ts\n@@ -1 +1 @@\n-x\n+y\n'
        )
        with patch('harness._apply_patch') as mock_apply:
            ok = harness._apply_and_validate(path, diff, suspect)
        self.assertFalse(ok)
        mock_apply.assert_not_called()

    def test_patch_not_touching_suspect_rejected_and_reverted(self):
        path = self._write_target()
        suspect = _make_suspect(file='app/(panel)/x/page.tsx', line=1)
        diff = (
            '--- a/lib/other.ts\n+++ b/lib/other.ts\n@@ -1 +1 @@\n-a\n+b\n'
        )
        with patch('harness._apply_patch', return_value=True), \
             patch('harness._revert_patch') as mock_revert, \
             patch('harness._run_tsc') as mock_tsc:
            ok = harness._apply_and_validate(path, diff, suspect)
        self.assertFalse(ok)
        mock_revert.assert_called_once()
        mock_tsc.assert_not_called()

    def test_tsc_failure_returns_false(self):
        path = self._write_target()
        suspect = _make_suspect(file='app/(panel)/x/page.tsx', line=1)
        with patch('harness._run_tsc', return_value=False), \
             patch('harness._run_smoke_subset') as mock_e2e:
            ok = harness._apply_and_validate(path, '// new', suspect)
        self.assertFalse(ok)
        mock_e2e.assert_not_called()

    def test_e2e_failure_returns_false(self):
        path = self._write_target()
        suspect = _make_suspect(file='app/(panel)/x/page.tsx', line=1)
        with patch('harness._run_tsc', return_value=True), \
             patch('harness._run_smoke_subset', return_value=False):
            ok = harness._apply_and_validate(path, '// new', suspect)
        self.assertFalse(ok)

    def test_both_gates_pass_returns_true(self):
        path = self._write_target()
        suspect = _make_suspect(file='app/(panel)/x/page.tsx', line=1)
        with patch('harness._run_tsc', return_value=True), \
             patch('harness._run_smoke_subset', return_value=True):
            ok = harness._apply_and_validate(path, '// new', suspect)
        self.assertTrue(ok)


# ---------------------------------------------------------------------------
# 8) fixer_worker (major branches, mocked)
# ---------------------------------------------------------------------------

class TestFixerWorker(unittest.TestCase):
    """fixer_worker: dedup, lock, no fixers, chain, fleet prepend, dry-run, risky."""

    def setUp(self):
        # args debe existir (lo usa fixer_worker para dry_run).
        if not hasattr(harness, 'args') or not isinstance(harness.args, argparse.Namespace):
            harness.args = argparse.Namespace(dry_run=False, no_e2e=False)
        harness.args.dry_run = False
        # Estado del thread limpio.
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()
        # File locks limpios (los usa fixer_worker).
        self._orig_locks = dict(harness._FILE_LOCKS)
        harness._FILE_LOCKS.clear()
        # Worktree real con un archivo de prueba.
        self.tmpdir = tempfile.mkdtemp(prefix='fixer-worker-')
        self._orig_wt = harness.WT
        self._orig_subdir = harness.SUBDIR
        self._orig_wtsub = harness.WT_SUBDIR
        self._orig_fleet = harness.FLEET
        harness.WT = Path(self.tmpdir)
        harness.SUBDIR = 'apps/dashboard'
        harness.WT_SUBDIR = harness.WT / harness.SUBDIR
        (harness.WT / harness.SUBDIR).mkdir(parents=True, exist_ok=True)
        self.target_rel = 'app/(panel)/clientes/page.tsx'
        self.target = harness.WT / harness.SUBDIR / self.target_rel
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_text('const x = 1;\n')
        # Stats snapshot para asserts incrementales.
        self._fixes_attempted = harness.STATS['fixes_attempted']
        self._fixes_succeeded = harness.STATS['fixes_succeeded']
        self._fixes_to_review = harness.STATS['fixes_to_review']

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

    # --- branches ---

    def test_dedup_skips_if_already_fixed(self):
        bug = _make_bug()
        rec = MagicMock()
        rec.fixed = True
        with patch.object(harness.MEM, 'get_bug', return_value=rec) as mock_get:
            harness.fixer_worker(bug)
        mock_get.assert_called_once()
        # No se incrementa fixes_attempted.
        self.assertEqual(harness.STATS['fixes_attempted'], self._fixes_attempted)

    def test_lock_contention_skips(self):
        bug = _make_bug()
        with patch.object(harness.MEM, 'get_bug', return_value=None):
            lock = harness._file_lock(bug.suspect.file)
            lock.acquire()  # otro fixer ya lo tiene.
            try:
                harness.fixer_worker(bug)
            finally:
                lock.release()
        # No se incrementa fixes_attempted.
        self.assertEqual(harness.STATS['fixes_attempted'], self._fixes_attempted)

    def test_no_fixers_logs_and_returns(self):
        bug = _make_bug()
        with patch.object(harness.MEM, 'get_bug', return_value=None), \
             patch('harness.quota.filter_tier', return_value=[]), \
             patch('harness.FLEET', None):
            harness.fixer_worker(bug)
        # No se hace commit, no se sube fixes_succeeded.
        self.assertEqual(harness.STATS['fixes_succeeded'], self._fixes_succeeded)

    def test_chains_until_one_succeeds(self):
        bug = _make_bug()
        spec_a = _make_fixer_spec('codex:gpt-5.5')
        spec_b = _make_fixer_spec('minimax/MiniMax-M3')
        with patch.object(harness.MEM, 'get_bug', return_value=None), \
             patch('harness.quota.filter_tier', return_value=[spec_a, spec_b]), \
             patch('harness.FLEET', None), \
             patch('harness.call_model', side_effect=['still same as original', 'fixed!']), \
             patch('harness._apply_and_validate', return_value=True), \
             patch('harness._commit_fix', return_value='cafebabe1234'):
            harness.fixer_worker(bug)
        # A: 'still same as original' → skip (== original). B: 'fixed!' → OK.
        self.assertEqual(harness.STATS['fixes_succeeded'], self._fixes_succeeded + 1)
        self.assertEqual(harness.STATS['fixes_attempted'], self._fixes_attempted + 1)

    def test_fleet_fixer_prepended(self):
        bug = _make_bug()
        local_fixer = _make_fixer_spec('ollama:qwen3-coder:30b')
        cloud_fixer = _make_fixer_spec('codex:gpt-5.5')
        mock_fleet = MagicMock()
        mock_fleet.fixer_spec.return_value = local_fixer
        calls = []
        def fake_call_model(spec, *args, **kw):
            calls.append(spec.id)
            return 'fix-from-' + spec.id
        with patch.object(harness.MEM, 'get_bug', return_value=None), \
             patch('harness.quota.filter_tier', return_value=[cloud_fixer, local_fixer]), \
             patch('harness.FLEET', mock_fleet), \
             patch('harness.call_model', side_effect=fake_call_model), \
             patch('harness._apply_and_validate', return_value=True), \
             patch('harness._commit_fix', return_value='abc1234'):
            harness.fixer_worker(bug)
        # El fixer local del fleet debe ir primero.
        self.assertEqual(calls[0], 'ollama:qwen3-coder:30b')

    def test_marks_done_status_on_completion(self):
        bug = _make_bug()
        spec = _make_fixer_spec()
        with patch.object(harness.MEM, 'get_bug', return_value=None), \
             patch('harness.quota.filter_tier', return_value=[spec]), \
             patch('harness.FLEET', None), \
             patch('harness.call_model', return_value='fixed content'), \
             patch('harness._apply_and_validate', return_value=True), \
             patch('harness._commit_fix', return_value='deadbeef'):
            harness.fixer_worker(bug)
        key = f'fixer-{bug.suspect.file}'
        with harness.THREAD_STATES_LOCK:
            st = harness.THREAD_STATES.get(key, {})
        self.assertEqual(st.get('status'), 'done')
        self.assertEqual(st.get('current_task'), 'OK')

    def test_records_bug_on_success(self):
        bug = _make_bug()
        spec = _make_fixer_spec()
        with patch.object(harness.MEM, 'get_bug', return_value=None), \
             patch.object(harness.MEM, 'record_bug') as mock_record, \
             patch('harness.quota.filter_tier', return_value=[spec]), \
             patch('harness.FLEET', None), \
             patch('harness.call_model', return_value='fixed content'), \
             patch('harness._apply_and_validate', return_value=True), \
             patch('harness._commit_fix', return_value='feedface0001'):
            harness.fixer_worker(bug)
        mock_record.assert_called_once()
        # record_bug(file, line, snippet, fix_commit=...) — los 3 primeros son
        # posicionales, fix_commit es kw.
        args, kwargs = mock_record.call_args
        self.assertEqual(kwargs.get('fix_commit'), 'feedface0001')
        self.assertEqual(args[0], bug.suspect.file)
        self.assertEqual(args[1], bug.suspect.line)
        self.assertEqual(args[2], bug.suspect.snippet)

    def test_adds_to_review_if_risky(self):
        # Usamos 'caja' como zone (caja está en RISKY_KEYWORDS) y la why
        # contiene 'dinero' para doble gatillo. Creamos el archivo físico
        # para que fixer_worker no retorne temprano.
        risky_rel = 'app/(panel)/caja/page.tsx'
        risky_path = harness.WT / harness.SUBDIR / risky_rel
        risky_path.parent.mkdir(parents=True, exist_ok=True)
        risky_path.write_text('const total = 0;\n')

        suspect = _make_suspect(
            file=risky_rel,
            why='manejo de dinero incorrecto',
            snippet='const total = await sumPagos()',
        )
        bug = _make_bug(suspect=suspect)
        spec = _make_fixer_spec()
        with patch.object(harness.MEM, 'get_bug', return_value=None), \
             patch('harness.quota.filter_tier', return_value=[spec]), \
             patch('harness.FLEET', None), \
             patch('harness.call_model', return_value='fixed content'), \
             patch('harness._apply_and_validate', return_value=True), \
             patch('harness._commit_fix', return_value='risky1234'), \
             patch('harness._add_to_review') as mock_review, \
             patch('harness.REVIEW_FILE', new=Path(tempfile.mkstemp(suffix='.json')[1])):
            harness.fixer_worker(bug)
        mock_review.assert_called_once()
        # mock_review fue llamado con (suspect, commit, model).
        args = mock_review.call_args[0]
        self.assertEqual(args[0], suspect)
        self.assertEqual(args[1], 'risky1234')
        self.assertEqual(args[2], 'codex:gpt-5.5')
        self.assertEqual(harness.STATS['fixes_to_review'], self._fixes_to_review + 1)

    def test_reverts_on_validation_failure(self):
        bug = _make_bug()
        spec = _make_fixer_spec()
        original = self.target.read_text()
        with patch.object(harness.MEM, 'get_bug', return_value=None), \
             patch('harness.quota.filter_tier', return_value=[spec]), \
             patch('harness.FLEET', None), \
             patch('harness.call_model', return_value='BAD CONTENT\n'), \
             patch('harness._apply_and_validate', return_value=False):
            harness.fixer_worker(bug)
        # El caller revierte: el archivo queda igual a como estaba.
        self.assertEqual(self.target.read_text(), original)
        # No se subió fixes_succeeded.
        self.assertEqual(harness.STATS['fixes_succeeded'], self._fixes_succeeded)

    def test_dry_run_marks_succeeded_without_commit(self):
        bug = _make_bug()
        spec = _make_fixer_spec()
        harness.args.dry_run = True
        with patch.object(harness.MEM, 'get_bug', return_value=None), \
             patch.object(harness.MEM, 'record_bug') as mock_record, \
             patch('harness.quota.filter_tier', return_value=[spec]), \
             patch('harness.FLEET', None), \
             patch('harness.call_model', return_value='DRY content\n'), \
             patch('harness._apply_and_validate') as mock_av, \
             patch('harness._commit_fix') as mock_commit:
            harness.fixer_worker(bug)
        # En dry-run, NO se llama _apply_and_validate ni _commit_fix.
        mock_av.assert_not_called()
        mock_commit.assert_not_called()
        # Se marca succeeded y se graba el bug con 'DRY-RUN'.
        self.assertEqual(harness.STATS['fixes_succeeded'], self._fixes_succeeded + 1)
        kwargs = mock_record.call_args.kwargs
        self.assertEqual(kwargs['fix_commit'], 'DRY-RUN')

    def test_no_change_skips_to_next_spec(self):
        bug = _make_bug()
        spec_a = _make_fixer_spec('codex:gpt-5.5')
        spec_b = _make_fixer_spec('minimax/MiniMax-M3')
        # A devuelve el mismo contenido que ya está en el archivo → strip()
        # coincide con original → skip al siguiente. B sí cambia → OK.
        with patch.object(harness.MEM, 'get_bug', return_value=None), \
             patch('harness.quota.filter_tier', return_value=[spec_a, spec_b]), \
             patch('harness.FLEET', None), \
             patch('harness.call_model', side_effect=['const x = 1;', 'actual fix']), \
             patch('harness._apply_and_validate', return_value=True), \
             patch('harness._commit_fix', return_value='ok1234567890') as mock_commit:
            harness.fixer_worker(bug)
        # Se hizo UN commit, con el spec que sí cambió.
        self.assertEqual(mock_commit.call_count, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
