#!/usr/bin/env python3
"""Tests del orquestador de prompts (orchestrator.py).

Cubre: dataclasses, lectura segura de archivos, scanners de rutas/actions/schema/
estado/auth, snapshot del proyecto, llamada al modelo, cache de prompts, fallbacks
heurísticos y journeys E2E.

Correr:  python3 -m unittest tests.test_orchestrator -v
         python3 -m unittest discover -s tests -p "test_*.py"
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orchestrator  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    """Escribe un archivo creando los directorios padre si hace falta."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


class _OrchestratorFixture(unittest.TestCase):
    """setUp común: crea un worktree mínimo con app/, lib/db/, lib/auth, etc."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.worktree = self.tmp.name
        self.subdir = 'apps/dashboard'
        self.root = Path(self.worktree) / self.subdir

        # app/(panel)/clientes/page.tsx
        _write(
            self.root / 'app/(panel)/clientes/page.tsx',
            "'use client';\nimport { auth } from '@/lib/auth';\n"
            "export default function Page() { return <div>clientes</div>; }\n",
        )
        # app/(panel)/pedidos/page.tsx
        _write(
            self.root / 'app/(panel)/pedidos/page.tsx',
            "export const dynamic = 'force-dynamic';\n"
            "export default function Page() { return <div>pedidos</div>; }\n",
        )
        # app/(panel)/clientes/actions.ts (sin zod, sin requireRole)
        _write(
            self.root / 'app/(panel)/clientes/actions.ts',
            "'use server';\nexport async function crearCliente(data: any) {\n"
            "  await db.insert(clientes).values(data);\n}\n",
        )
        # app/(panel)/pedidos/actions.ts (con requireRole)
        _write(
            self.root / 'app/(panel)/pedidos/actions.ts',
            "'use server';\nimport { requireRole } from '@/lib/auth';\n"
            "import { z } from 'zod';\n"
            "const schema = z.object({ clienteId: z.string() });\n"
            "export async function crearPedido(input: unknown) {\n"
            "  await requireRole('vendedor');\n"
            "  const data = schema.parse(input);\n}\n",
        )
        # lib/db/schema.ts
        _write(
            self.root / 'lib/db/schema.ts',
            "import { pgTable, pgEnum } from 'drizzle-orm/pg-core';\n"
            "export const clientes = pgTable('clientes', { id: 0 });\n"
            "export const pedidos = pgTable('pedidos', { id: 0 });\n"
            "export const estadoPedido = pgEnum('estado_pedido', ['recibido']);\n",
        )
        # lib/orders/estado.ts
        _write(
            self.root / 'lib/orders/estado.ts',
            "export const ESTADOS = ['recibido', 'aceptado', 'en_cola'] as const;\n"
            "export function siguiente(actual: string) { return 'aceptado'; }\n",
        )
        # lib/auth.ts
        _write(
            self.root / 'lib/auth.ts',
            "export async function requireRole(rol: string) { return true; }\n"
            "export async function requirePermission(p: string) { return true; }\n",
        )

        # Cache por defecto dentro del tmp (evita pisar /tmp entre tests).
        self.cache_path = str(Path(self.worktree) / 'orch-prompts.json')

    def tearDown(self) -> None:
        self.tmp.cleanup()


# ---------------------------------------------------------------------------
# 1-4: _read_file_safe
# ---------------------------------------------------------------------------


class TestReadFileSafe(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_returns_content_for_existing_text_file(self) -> None:
        path = Path(self.tmp.name) / 'a.txt'
        path.write_text('hola mundo\nsegunda línea\n', encoding='utf-8')
        self.assertEqual(orchestrator._read_file_safe(str(path)), 'hola mundo\nsegunda línea\n')

    def test_returns_empty_for_missing_file(self) -> None:
        self.assertEqual(orchestrator._read_file_safe('/tmp/does-not-exist-xyz-123'), '')

    def test_returns_empty_for_binary_file(self) -> None:
        """Un archivo con bytes no-utf8 debe devolver '' (UnicodeDecodeError)."""
        path = Path(self.tmp.name) / 'bin.dat'
        path.write_bytes(b'\x00\xff\xfe\x80abc')
        self.assertEqual(orchestrator._read_file_safe(str(path)), '')

    def test_truncates_at_max_lines(self) -> None:
        path = Path(self.tmp.name) / 'many.txt'
        path.write_text('\n'.join(f'línea {i}' for i in range(50)), encoding='utf-8')
        out = orchestrator._read_file_safe(str(path), max_lines=5)
        # El archivo original tiene 50 líneas; tras truncar a 5, debe haber 5.
        self.assertEqual(len(out.splitlines()), 5)
        self.assertTrue(out.startswith('línea 0'))
        self.assertNotIn('línea 5', out)

    def test_truncation_preserves_max_lines_even_if_some_have_no_newline(self) -> None:
        path = Path(self.tmp.name) / 'mixed.txt'
        # 10 líneas SIN newline final.
        path.write_text('\n'.join(f'x{i}' for i in range(10)), encoding='utf-8')
        out = orchestrator._read_file_safe(str(path), max_lines=3)
        self.assertEqual(len(out.splitlines()), 3)
        self.assertIn('x0', out)
        self.assertIn('x2', out)
        self.assertNotIn('x3', out)


# ---------------------------------------------------------------------------
# 5-8: _scan_routes, _scan_actions
# ---------------------------------------------------------------------------


class TestScanRoutes(_OrchestratorFixture):

    def test_finds_page_tsx_files(self) -> None:
        routes = orchestrator._scan_routes(str(self.root / 'app'))
        self.assertEqual(len(routes), 2)
        joined = ' '.join(routes)
        self.assertIn('clientes/page.tsx', joined)
        self.assertIn('pedidos/page.tsx', joined)

    def test_returns_sorted_paths(self) -> None:
        routes = orchestrator._scan_routes(str(self.root / 'app'))
        # Verificar orden lexicográfico.
        self.assertEqual(routes, sorted(routes))
        # Y que la primera sea clientes (c < p).
        self.assertTrue(routes[0].endswith('clientes/page.tsx'))

    def test_returns_empty_list_for_missing_subdir(self) -> None:
        self.assertEqual(orchestrator._scan_routes('/tmp/does-not-exist-xyz-987'), [])


class TestScanActions(_OrchestratorFixture):

    def test_finds_actions_ts(self) -> None:
        actions = orchestrator._scan_actions(str(self.root / 'app'))
        self.assertEqual(len(actions), 2)
        joined = ' '.join(actions)
        self.assertIn('clientes/actions.ts', joined)
        self.assertIn('pedidos/actions.ts', joined)

    def test_also_finds_lib_actions_fallback(self) -> None:
        """_scan_actions NO mira lib/, pero build_project_snapshot sí como fallback.
        Verificamos que el fallback del snapshot funciona."""
        _write(
            self.root / 'lib/actions.ts',
            "'use server';\nexport async function foo() {}\n",
        )
        # _scan_actions sólo busca desde subdir, así que tiene que pasarse 'lib'.
        actions = orchestrator._scan_actions(str(self.root / 'lib'))
        self.assertIn('actions.ts', actions)


# ---------------------------------------------------------------------------
# 9-12: _scan_schema, _scan_estado, _scan_auth
# ---------------------------------------------------------------------------


class TestScanSchema(_OrchestratorFixture):

    def test_reads_lib_db_schema_ts(self) -> None:
        schema = orchestrator._scan_schema(str(self.root))
        self.assertIn('pgTable', schema)
        self.assertIn('clientes', schema)
        self.assertIn('pedidos', schema)


class TestScanEstado(_OrchestratorFixture):

    def test_finds_estado_ts(self) -> None:
        estado = orchestrator._scan_estado(str(self.root))
        self.assertIn('ESTADOS', estado)
        self.assertIn('recibido', estado)


class TestScanAuth(_OrchestratorFixture):

    def test_finds_auth_ts(self) -> None:
        auth = orchestrator._scan_auth(str(self.root))
        self.assertIn('requireRole', auth)
        self.assertIn('requirePermission', auth)

    def test_returns_empty_when_missing(self) -> None:
        # Usar un subdir sin auth.
        empty_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(empty_tmp.cleanup)
        self.assertEqual(orchestrator._scan_auth(empty_tmp.name), '')


# ---------------------------------------------------------------------------
# 13-16: build_project_snapshot
# ---------------------------------------------------------------------------


class TestBuildProjectSnapshot(_OrchestratorFixture):

    def test_returns_non_empty_string_for_valid_worktree(self) -> None:
        snap = orchestrator.build_project_snapshot(self.worktree, self.subdir)
        self.assertIsInstance(snap, str)
        self.assertGreater(len(snap), 100)

    def test_includes_snapshot_header(self) -> None:
        snap = orchestrator.build_project_snapshot(self.worktree, self.subdir)
        self.assertIn('Snapshot', snap)
        self.assertIn('# Proyecto Deméter', snap)
        self.assertIn('## Rutas (page.tsx)', snap)
        self.assertIn('## Server Actions', snap)
        self.assertIn('## Schema (Drizzle)', snap)
        self.assertIn('## Máquina de Estados', snap)
        self.assertIn('## Auth / Permisos', snap)

    def test_returns_empty_for_missing_worktree(self) -> None:
        self.assertEqual(
            orchestrator.build_project_snapshot('/tmp/no-such-xyz-456', 'apps/x'),
            '',
        )

    def test_truncates_if_too_long(self) -> None:
        """Si el snapshot excede MAX_SNAPSHOT_LINES, se trunca con marcador.

        Usamos un mock del límite (en lugar de inflar el árbol) porque cada page.tsx
        se trunca a 8 líneas y el cap real es 8000 — necesitaríamos 1000+ archivos.
        """
        with patch.object(orchestrator, 'MAX_SNAPSHOT_LINES', 30):
            snap = orchestrator.build_project_snapshot(self.worktree, self.subdir)
        self.assertLessEqual(
            len(snap.splitlines()),
            orchestrator.MAX_SNAPSHOT_LINES + 1,  # +1 por la línea de "... (truncado..."
        )
        self.assertIn('truncado', snap)

    def test_snapshot_includes_route_context_section(self) -> None:
        snap = orchestrator.build_project_snapshot(self.worktree, self.subdir)
        self.assertIn('## Contexto por ruta', snap)
        self.assertIn('clientes/page.tsx', snap)


# ---------------------------------------------------------------------------
# 17-22: call_orchestrator
# ---------------------------------------------------------------------------


class TestCallOrchestrator(unittest.TestCase):

    def setUp(self) -> None:
        # Forzar el path de opencode (default: 'minimax/MiniMax-M3').
        self._orig_model = orchestrator.ORCHESTRATOR_MODEL
        orchestrator.ORCHESTRATOR_MODEL = 'minimax/MiniMax-M3'

    def tearDown(self) -> None:
        orchestrator.ORCHESTRATOR_MODEL = self._orig_model

    def test_returns_none_for_empty_snapshot(self) -> None:
        self.assertIsNone(orchestrator.call_orchestrator(''))

    def test_returns_parsed_dict_from_mocked_subprocess(self) -> None:
        payload = {
            'hunting_prompts': [{'file': 'a.ts', 'line': 1, 'reason': 'x'}],
            'e2e_journeys': [],
        }
        mock_proc = MagicMock()
        mock_proc.stdout = json.dumps(payload)
        mock_proc.stderr = ''
        with patch('orchestrator.subprocess.run', return_value=mock_proc) as run_mock:
            result = orchestrator.call_orchestrator('snapshot no vacío')
        run_mock.assert_called_once()
        self.assertEqual(result, payload)

    def test_extracts_json_from_code_block(self) -> None:
        """Si la salida es texto con ```json ... ```, extrae el JSON del bloque."""
        body = json.dumps({'hunting_prompts': [{'file': 'b.ts', 'line': 2, 'reason': 'y'}]})
        wrapped = f'Respuesta del modelo:\n```json\n{body}\n```\nFin.'
        mock_proc = MagicMock()
        mock_proc.stdout = wrapped
        with patch('orchestrator.subprocess.run', return_value=mock_proc):
            result = orchestrator.call_orchestrator('snap')
        self.assertIsNotNone(result)
        self.assertEqual(result['hunting_prompts'][0]['file'], 'b.ts')

    def test_returns_none_for_non_json_output(self) -> None:
        mock_proc = MagicMock()
        mock_proc.stdout = 'Esto no es JSON en absoluto. Es prosa del modelo.'
        with patch('orchestrator.subprocess.run', return_value=mock_proc):
            self.assertIsNone(orchestrator.call_orchestrator('snap'))

    def test_returns_none_on_filenotfound(self) -> None:
        with patch('orchestrator.subprocess.run', side_effect=FileNotFoundError):
            self.assertIsNone(orchestrator.call_orchestrator('snap'))

    def test_returns_none_on_timeout(self) -> None:
        with patch(
            'orchestrator.subprocess.run',
            side_effect=subprocess.TimeoutExpired(cmd='opencode', timeout=1),
        ):
            self.assertIsNone(orchestrator.call_orchestrator('snap'))


# ---------------------------------------------------------------------------
# 23-26: generate_hunting_prompts
# ---------------------------------------------------------------------------


class TestGenerateHuntingPrompts(_OrchestratorFixture):

    def _write_cache(self, content: dict, mtime: float | None = None) -> None:
        Path(self.cache_path).write_text(json.dumps(content), encoding='utf-8')
        if mtime is not None:
            os.utime(self.cache_path, (mtime, mtime))

    def test_uses_cache_if_fresh(self) -> None:
        self._write_cache({
            'hunting_prompts': [
                {'file': 'cached.ts', 'line': 7, 'reason': 'cached', 'context': 'ctx'},
            ],
            'e2e_journeys': [],
        }, mtime=time.time() - 60)  # 1 minuto de antigüedad
        with patch('orchestrator.call_orchestrator') as mock_call:
            prompts = orchestrator.generate_hunting_prompts(
                self.worktree, self.subdir, cache_path=self.cache_path,
            )
        mock_call.assert_not_called()
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0].file, 'cached.ts')
        self.assertEqual(prompts[0].line, 7)
        self.assertEqual(prompts[0].reason, 'cached')

    def test_regenerates_if_cache_is_old(self) -> None:
        self._write_cache({
            'hunting_prompts': [
                {'file': 'stale.ts', 'line': 1, 'reason': 'old', 'context': ''},
            ],
            'e2e_journeys': [],
        }, mtime=time.time() - 7200)  # 2h, fuera de cache
        payload = {
            'hunting_prompts': [{'file': 'new.ts', 'line': 9, 'reason': 'new'}],
            'e2e_journeys': [],
        }
        mock_proc = MagicMock()
        mock_proc.stdout = json.dumps(payload)
        with patch('orchestrator.subprocess.run', return_value=mock_proc):
            prompts = orchestrator.generate_hunting_prompts(
                self.worktree, self.subdir, cache_path=self.cache_path,
            )
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0].file, 'new.ts')

    def test_regenerates_if_cache_is_malformed(self) -> None:
        Path(self.cache_path).write_text('esto NO es json {{{ roto', encoding='utf-8')
        os.utime(self.cache_path, (time.time() - 60, time.time() - 60))
        payload = {
            'hunting_prompts': [{'file': 'fresh.ts', 'line': 3, 'reason': 'regen'}],
            'e2e_journeys': [],
        }
        mock_proc = MagicMock()
        mock_proc.stdout = json.dumps(payload)
        with patch('orchestrator.subprocess.run', return_value=mock_proc):
            prompts = orchestrator.generate_hunting_prompts(
                self.worktree, self.subdir, cache_path=self.cache_path,
            )
        self.assertEqual(prompts[0].file, 'fresh.ts')

    def test_falls_back_when_model_fails(self) -> None:
        # Sin cache → build snapshot → call_orchestrator → None → fallback.
        with patch('orchestrator.call_orchestrator', return_value=None):
            prompts = orchestrator.generate_hunting_prompts(
                self.worktree, self.subdir, cache_path=self.cache_path,
            )
        # El fallback sobre clientes/actions.ts (sin zod, sin requireRole) debe
        # generar al menos un prompt de tipo auth-missing.
        self.assertGreater(len(prompts), 0)
        reasons = ' '.join(p.reason for p in prompts)
        self.assertTrue(
            any('auth' in r.lower() for r in reasons.split()),
            f'fallback debería mencionar auth. reasons={list(reasons.split())}',
        )

    def test_returns_empty_when_worktree_missing(self) -> None:
        prompts = orchestrator.generate_hunting_prompts(
            '/tmp/no-such-xyz-789', 'apps/x', cache_path=self.cache_path,
        )
        self.assertEqual(prompts, [])


# ---------------------------------------------------------------------------
# 27-28: _fallback_prompts
# ---------------------------------------------------------------------------


class TestFallbackPrompts(_OrchestratorFixture):

    def test_finds_actions_without_auth(self) -> None:
        """clientes/actions.ts no tiene requireRole → debe generar prompt de auth."""
        prompts = orchestrator._fallback_prompts(self.worktree, self.subdir)
        # Buscar un prompt sobre clientes/actions.ts que mencione auth.
        auth_prompts = [p for p in prompts if 'clientes/actions.ts' in p.file]
        self.assertGreater(len(auth_prompts), 0)
        self.assertTrue(
            any('auth' in p.reason.lower() for p in auth_prompts),
            f'se esperaba prompt de auth en clientes/actions.ts. reasons={[p.reason for p in auth_prompts]}',
        )

    def test_returns_at_most_30_prompts(self) -> None:
        # Crear muchos actions.ts sin auth/zod para superar el límite.
        for i in range(20):
            _write(
                self.root / f'app/(panel)/mod{i}/actions.ts',
                "'use server';\nexport async function f() {}\n",
            )
            _write(
                self.root / f'app/(panel)/mod{i}/page.tsx',
                "// sin export const dynamic\n",
            )
        prompts = orchestrator._fallback_prompts(self.worktree, self.subdir)
        self.assertLessEqual(len(prompts), 30)

    def test_skips_actions_with_zod_and_auth(self) -> None:
        """pedidos/actions.ts tiene requireRole + zod → NO debe generar prompt de fallback."""
        prompts = orchestrator._fallback_prompts(self.worktree, self.subdir)
        pedidos_auth_or_zod = [
            p for p in prompts
            if 'pedidos/actions.ts' in p.file
            and ('auth' in p.reason.lower() or 'zod' in p.reason.lower())
        ]
        self.assertEqual(
            pedidos_auth_or_zod, [],
            f'pedidos/actions.ts tiene auth+zod, no debería aparecer. got={pedidos_auth_or_zod}',
        )


# ---------------------------------------------------------------------------
# 29-31: _fallback_e2e, generate_e2e_journeys
# ---------------------------------------------------------------------------


class TestFallbackE2E(unittest.TestCase):

    def test_returns_non_empty_list_with_canonical_journey(self) -> None:
        journeys = orchestrator._fallback_e2e()
        self.assertIsInstance(journeys, list)
        self.assertGreater(len(journeys), 0)
        names = [j.name for j in journeys]
        self.assertIn('Pedido → Despacho completo', names)
        self.assertTrue(any(j.module for j in journeys))

    def test_journey_has_steps_and_expected(self) -> None:
        journeys = orchestrator._fallback_e2e()
        for j in journeys:
            self.assertIsInstance(j.steps, list)
            self.assertGreater(len(j.steps), 0)
            self.assertIsInstance(j.expected, str)
            self.assertGreater(len(j.expected), 0)


class TestGenerateE2EJourneys(_OrchestratorFixture):

    def test_uses_cache(self) -> None:
        cached = [
            {'name': 'cached journey', 'module': 'mod',
             'steps': ['step1'], 'expected': 'ok'},
        ]
        Path(self.cache_path).write_text(
            json.dumps({'hunting_prompts': [], 'e2e_journeys': cached}),
            encoding='utf-8',
        )
        os.utime(self.cache_path, (time.time() - 60, time.time() - 60))
        with patch('orchestrator.call_orchestrator') as mock_call:
            journeys = orchestrator.generate_e2e_journeys(
                self.worktree, self.subdir, cache_path=self.cache_path,
            )
        mock_call.assert_not_called()
        self.assertEqual(len(journeys), 1)
        self.assertEqual(journeys[0].name, 'cached journey')

    def test_falls_back_when_model_fails(self) -> None:
        with patch('orchestrator.call_orchestrator', return_value=None):
            journeys = orchestrator.generate_e2e_journeys(
                self.worktree, self.subdir, cache_path=self.cache_path,
            )
        self.assertGreater(len(journeys), 0)
        self.assertIn('Pedido → Despacho completo', [j.name for j in journeys])

    def test_uses_model_when_cache_missing(self) -> None:
        payload = {
            'hunting_prompts': [],
            'e2e_journeys': [
                {'name': 'model journey', 'module': 'x',
                 'steps': ['a', 'b'], 'expected': 'ok'},
            ],
        }
        mock_proc = MagicMock()
        mock_proc.stdout = json.dumps(payload)
        with patch('orchestrator.subprocess.run', return_value=mock_proc):
            journeys = orchestrator.generate_e2e_journeys(
                self.worktree, self.subdir, cache_path=self.cache_path,
            )
        self.assertEqual(len(journeys), 1)
        self.assertEqual(journeys[0].name, 'model journey')


# ---------------------------------------------------------------------------
# 32-34: Dataclass fields
# ---------------------------------------------------------------------------


class TestDataclasses(unittest.TestCase):

    def test_hunting_prompt_fields(self) -> None:
        f = orchestrator.HuntingPrompt.__dataclass_fields__
        self.assertIn('file', f)
        self.assertIn('line', f)
        self.assertIn('reason', f)
        self.assertIn('context', f)
        p = orchestrator.HuntingPrompt(file='x.ts', line=10, reason='why', context='ctx')
        self.assertEqual(p.file, 'x.ts')
        self.assertEqual(p.line, 10)
        self.assertEqual(p.reason, 'why')
        self.assertEqual(p.context, 'ctx')
        # context default.
        p2 = orchestrator.HuntingPrompt(file='y.ts', line=1, reason='r')
        self.assertEqual(p2.context, '')

    def test_e2e_journey_fields(self) -> None:
        f = orchestrator.E2EJourney.__dataclass_fields__
        self.assertIn('name', f)
        self.assertIn('steps', f)
        self.assertIn('expected', f)
        self.assertIn('module', f)
        j = orchestrator.E2EJourney(
            name='n', steps=['s1'], expected='e', module='m',
        )
        self.assertEqual(j.name, 'n')
        self.assertEqual(j.steps, ['s1'])
        self.assertEqual(j.expected, 'e')
        self.assertEqual(j.module, 'm')
        # module default.
        j2 = orchestrator.E2EJourney(name='n2', steps=[], expected='')
        self.assertEqual(j2.module, '')

    def test_domain_map_fields(self) -> None:
        f = orchestrator.DomainMap.__dataclass_fields__
        self.assertIn('modules', f)
        self.assertIn('state_machines', f)
        self.assertIn('roles', f)
        self.assertIn('auth_checks', f)
        self.assertIn('risky_routes', f)
        self.assertIn('forms', f)
        self.assertIn('relations', f)
        m = orchestrator.DomainMap()
        self.assertEqual(m.modules, [])
        self.assertEqual(m.state_machines, {})
        self.assertEqual(m.roles, [])
        self.assertEqual(m.auth_checks, [])
        self.assertEqual(m.risky_routes, [])
        self.assertEqual(m.forms, [])
        self.assertEqual(m.relations, [])


# ---------------------------------------------------------------------------
# Extra: ORCHESTRATOR_SYSTEM constant
# ---------------------------------------------------------------------------


class TestOrchestratorSystem(unittest.TestCase):

    def test_is_string_with_required_keys(self) -> None:
        s = orchestrator.ORCHESTRATOR_SYSTEM
        self.assertIsInstance(s, str)
        self.assertIn('hunting_prompts', s)
        self.assertIn('e2e_journeys', s)
        self.assertIn('Auth', s)
        self.assertIn('Tenant', s)
        self.assertIn('Estados', s)


if __name__ == '__main__':
    unittest.main(verbosity=2)
