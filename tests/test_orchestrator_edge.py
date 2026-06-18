#!/usr/bin/env python3
"""Edge-case tests para orchestrator.py.

Cubre: archivos vacíos, PermissionError, codificaciones extrañas, subdirs
vacíos, branch de Gemini CLI, modelo desconocido, ruido de build, cache
parcialmente corrupto, fallbacks con cero archivos, dataclasses equality y
comportamiento del snapshot cuando faltan secciones.

Correr:  python3 -m unittest tests.test_orchestrator_edge -v
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import orchestrator  # noqa: E402


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


# ---------------------------------------------------------------------------
# _read_file_safe — bordes
# ---------------------------------------------------------------------------


class TestReadFileSafeEdge(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_permission_error_returns_empty(self) -> None:
        """Un PermissionError al abrir debe devolver '' (no propagar)."""
        with patch('builtins.open', side_effect=PermissionError('denied')):
            self.assertEqual(orchestrator._read_file_safe('/algo/irrelevante'), '')

    def test_empty_file_returns_empty_string(self) -> None:
        path = Path(self.tmp.name) / 'empty.txt'
        path.write_text('', encoding='utf-8')
        self.assertEqual(orchestrator._read_file_safe(str(path)), '')

    def test_max_lines_zero_returns_empty(self) -> None:
        path = Path(self.tmp.name) / 'a.txt'
        path.write_text('línea 1\nlínea 2\n', encoding='utf-8')
        self.assertEqual(orchestrator._read_file_safe(str(path), max_lines=0), '')

    def test_max_lines_larger_than_file_returns_full_file(self) -> None:
        path = Path(self.tmp.name) / 'small.txt'
        content = 'a\nb\nc'
        path.write_text(content, encoding='utf-8')
        self.assertEqual(orchestrator._read_file_safe(str(path), max_lines=1000), content + '')

    def test_max_lines_one_returns_first_line(self) -> None:
        path = Path(self.tmp.name) / 'a.txt'
        path.write_text('primera\nsegunda\ntercera\n', encoding='utf-8')
        out = orchestrator._read_file_safe(str(path), max_lines=1)
        self.assertEqual(out, 'primera\n')

    def test_unicode_content_preserved(self) -> None:
        path = Path(self.tmp.name) / 'utf8.txt'
        path.write_text('café — ñ — 中文\nsegunda\n', encoding='utf-8')
        out = orchestrator._read_file_safe(str(path))
        self.assertIn('café', out)
        self.assertIn('中文', out)


# ---------------------------------------------------------------------------
# Scanners — bordes
# ---------------------------------------------------------------------------


class TestScannerEdge(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_scan_routes_empty_subdir(self) -> None:
        empty = self.root / 'vacio'
        empty.mkdir()
        self.assertEqual(orchestrator._scan_routes(str(empty)), [])

    def test_scan_routes_deeply_nested(self) -> None:
        # (panel)/admin/configuracion/usuarios/[id]/page.tsx
        nested = self.root / 'app/(panel)/admin/configuracion/usuarios/[id]/page.tsx'
        _write(nested, 'export default function() {}')
        routes = orchestrator._scan_routes(str(self.root / 'app'))
        self.assertEqual(len(routes), 1)
        self.assertIn('usuarios/[id]/page.tsx', routes[0])

    def test_scan_routes_finds_nested_layout_groups(self) -> None:
        """Varios grupos de paréntesis paralelos."""
        (self.root / 'app/(marketing)/landing/page.tsx').parent.mkdir(parents=True, exist_ok=True)
        (self.root / 'app/(panel)/admin/page.tsx').parent.mkdir(parents=True, exist_ok=True)
        _write(self.root / 'app/(marketing)/landing/page.tsx', '//')
        _write(self.root / 'app/(panel)/admin/page.tsx', '//')
        routes = orchestrator._scan_routes(str(self.root / 'app'))
        self.assertEqual(len(routes), 2)
        # El orden debe ser estable (sorted).
        self.assertEqual(routes, sorted(routes))

    def test_scan_actions_empty_subdir(self) -> None:
        empty = self.root / 'vacio'
        empty.mkdir()
        self.assertEqual(orchestrator._scan_actions(str(empty)), [])

    def test_scan_actions_finds_actions_in_multiple_modules(self) -> None:
        for mod in ('clientes', 'pedidos', 'cartera'):
            _write(self.root / f'app/(panel)/{mod}/actions.ts', '//')
        actions = orchestrator._scan_actions(str(self.root / 'app'))
        self.assertEqual(len(actions), 3)
        self.assertEqual(actions, sorted(actions))

    def test_scan_schema_returns_empty_when_missing(self) -> None:
        self.assertEqual(orchestrator._scan_schema(str(self.root)), '')

    def test_scan_schema_picks_lib_db_schema(self) -> None:
        _write(self.root / 'lib/db/schema.ts', 'export const x = 1;\n' * 500)
        schema = orchestrator._scan_schema(str(self.root))
        # Cap a 400 líneas.
        self.assertEqual(len(schema.splitlines()), 400)

    def test_scan_estado_finds_alternative_paths(self) -> None:
        """Si no está en lib/orders, busca en lib/pedidos."""
        _write(self.root / 'lib/pedidos/estado.ts', '// estado v2')
        estado = orchestrator._scan_estado(str(self.root))
        self.assertIn('estado v2', estado)

    def test_scan_estado_fallback_to_lib_estado(self) -> None:
        _write(self.root / 'lib/estado.ts', '// root estado')
        estado = orchestrator._scan_estado(str(self.root))
        self.assertIn('root estado', estado)

    def test_scan_estado_returns_empty_when_missing(self) -> None:
        self.assertEqual(orchestrator._scan_estado(str(self.root)), '')

    def test_scan_auth_finds_auth_index_ts(self) -> None:
        _write(self.root / 'lib/auth/index.ts', 'export const RBAC = 1;')
        auth = orchestrator._scan_auth(str(self.root))
        self.assertIn('RBAC', auth)

    def test_scan_auth_returns_empty_when_missing(self) -> None:
        self.assertEqual(orchestrator._scan_auth(str(self.root)), '')


# ---------------------------------------------------------------------------
# build_project_snapshot — bordes
# ---------------------------------------------------------------------------


class TestSnapshotEdge(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.worktree = self.tmp.name

    def test_snapshot_with_only_routes(self) -> None:
        subdir = 'apps/x'
        _write(Path(self.worktree) / subdir / 'app/(panel)/a/page.tsx', '//')
        snap = orchestrator.build_project_snapshot(self.worktree, subdir)
        self.assertIn('## Rutas (page.tsx)', snap)
        # Sin schema/auth/estado: no deben aparecer los headers de esas secciones.
        self.assertNotIn('## Schema (Drizzle)', snap)
        self.assertNotIn('## Máquina de Estados', snap)
        self.assertNotIn('## Auth / Permisos', snap)

    def test_snapshot_with_schema_but_no_tables(self) -> None:
        """Schema sin `export const x = pg(Table|Enum)(` → no se listan tablas."""
        subdir = 'apps/x'
        _write(
            Path(self.worktree) / subdir / 'lib/db/schema.ts',
            '// archivo sin tablas Drizzle\nconst x = 1;\n',
        )
        snap = orchestrator.build_project_snapshot(self.worktree, subdir)
        self.assertIn('## Schema (Drizzle)', snap)
        self.assertNotIn('Tablas y enums:', snap)

    def test_snapshot_lists_tables_and_enums(self) -> None:
        subdir = 'apps/x'
        _write(
            Path(self.worktree) / subdir / 'lib/db/schema.ts',
            'export const users = pgTable("users", {});\n'
            'export const status = pgEnum("status", ["a","b"]);\n',
        )
        snap = orchestrator.build_project_snapshot(self.worktree, subdir)
        self.assertIn('users (Table)', snap)
        self.assertIn('status (Enum)', snap)

    def test_snapshot_includes_lib_actions_fallback(self) -> None:
        """Si no hay actions.ts en app/, busca lib/actions*.ts."""
        subdir = 'apps/x'
        _write(Path(self.worktree) / subdir / 'lib/actions.ts', '// fallback')
        snap = orchestrator.build_project_snapshot(self.worktree, subdir)
        self.assertIn('lib/actions.ts', snap)

    def test_snapshot_includes_auth_section(self) -> None:
        subdir = 'apps/x'
        _write(
            Path(self.worktree) / subdir / 'lib/auth.ts',
            'export function requireRole() {}\n' * 50,
        )
        snap = orchestrator.build_project_snapshot(self.worktree, self.subdir) if hasattr(self, 'subdir') else orchestrator.build_project_snapshot(self.worktree, subdir)
        self.assertIn('## Auth / Permisos', snap)
        self.assertIn('requireRole', snap)

    def test_snapshot_empty_worktree_returns_empty(self) -> None:
        self.assertEqual(
            orchestrator.build_project_snapshot(self.worktree, 'no/existe'),
            '',
        )

    def test_snapshot_includes_action_files(self) -> None:
        subdir = 'apps/x'
        _write(Path(self.worktree) / subdir / 'app/(panel)/caja/actions.ts', '//')
        snap = orchestrator.build_project_snapshot(self.worktree, subdir)
        self.assertIn('## Server Actions', snap)
        self.assertIn('caja/actions.ts', snap)
        self.assertIn('Total: 1 actions', snap)

    def test_snapshot_truncation_marker_format(self) -> None:
        subdir = 'apps/x'
        _write(Path(self.worktree) / subdir / 'app/(panel)/a/page.tsx', '//')
        with patch.object(orchestrator, 'MAX_SNAPSHOT_LINES', 10):
            snap = orchestrator.build_project_snapshot(self.worktree, subdir)
        self.assertIn('(truncado a 10 líneas)', snap)


# ---------------------------------------------------------------------------
# call_orchestrator — bordes (branches, ruido, modelos desconocidos)
# ---------------------------------------------------------------------------


class TestCallOrchestratorEdge(unittest.TestCase):

    def setUp(self) -> None:
        self._orig_model = orchestrator.ORCHESTRATOR_MODEL

    def tearDown(self) -> None:
        orchestrator.ORCHESTRATOR_MODEL = self._orig_model

    def test_unknown_model_prefix_returns_none(self) -> None:
        orchestrator.ORCHESTRATOR_MODEL = 'modelo-raro:abc'
        self.assertIsNone(orchestrator.call_orchestrator('snap'))

    def test_whitespace_only_snapshot_still_calls_subprocess(self) -> None:
        """El check `if not snapshot` solo cubre string vacío, no whitespace.

        Whitespace es truthy → llega hasta subprocess.run (mockeado acá).
        """
        orchestrator.ORCHESTRATOR_MODEL = 'minimax/MiniMax-M3'
        mock_proc = MagicMock()
        mock_proc.stdout = 'algo'
        with patch('orchestrator.subprocess.run', return_value=mock_proc) as run:
            orchestrator.call_orchestrator('   \n  \t  \n')
        run.assert_called_once()

    def test_gemini_branch_parses_response_field(self) -> None:
        orchestrator.ORCHESTRATOR_MODEL = 'gemini:gemini-2.5-flash-lite'
        inner = json.dumps({'hunting_prompts': [{'file': 'a.ts', 'line': 1, 'reason': 'x'}]})
        outer = json.dumps({'response': inner, 'session_id': 'abc', 'stats': {}})
        mock_proc = MagicMock()
        mock_proc.stdout = outer
        mock_proc.stderr = ''
        with patch('orchestrator.subprocess.run', return_value=mock_proc) as run:
            result = orchestrator.call_orchestrator('snap')
        run.assert_called_once()
        # La llamada debe haber sido con 'gemini' CLI.
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], 'gemini')
        self.assertIn('gemini-2.5-flash-lite', cmd)
        self.assertEqual(result['hunting_prompts'][0]['file'], 'a.ts')

    def test_gemini_branch_handles_filenotfound(self) -> None:
        orchestrator.ORCHESTRATOR_MODEL = 'gemini:test'
        with patch('orchestrator.subprocess.run', side_effect=FileNotFoundError):
            self.assertIsNone(orchestrator.call_orchestrator('snap'))

    def test_gemini_branch_handles_timeout(self) -> None:
        orchestrator.ORCHESTRATOR_MODEL = 'gemini:test'
        with patch(
            'orchestrator.subprocess.run',
            side_effect=subprocess.TimeoutExpired(cmd='gemini', timeout=1),
        ):
            self.assertIsNone(orchestrator.call_orchestrator('snap'))

    def test_gemini_branch_handles_invalid_outer_json(self) -> None:
        orchestrator.ORCHESTRATOR_MODEL = 'gemini:test'
        mock_proc = MagicMock()
        mock_proc.stdout = 'no es json'
        with patch('orchestrator.subprocess.run', return_value=mock_proc):
            self.assertIsNone(orchestrator.call_orchestrator('snap'))

    def test_opencode_filters_build_noise(self) -> None:
        """El output con líneas de build '[0m' o 'build · ...' debe limpiarse."""
        orchestrator.ORCHESTRATOR_MODEL = 'minimax/MiniMax-M3'
        clean = json.dumps({'hunting_prompts': []})
        noisy = f'[0mlínea build\nbuild · algo\n{clean}\n[0m más ruido\n'
        mock_proc = MagicMock()
        mock_proc.stdout = noisy
        with patch('orchestrator.subprocess.run', return_value=mock_proc):
            result = orchestrator.call_orchestrator('snap')
        self.assertEqual(result, {'hunting_prompts': []})

    def test_uses_rfind_fallback_when_codeblock_match_invalid(self) -> None:
        """Si el regex de code-block matchea JSON mal (no-greedy corta antes),
        el fallback de find/rfind debe capturar el JSON completo."""
        orchestrator.ORCHESTRATOR_MODEL = 'minimax/MiniMax-M3'
        # JSON anidado: el regex no-greedy cortaría mal.
        valid = json.dumps({
            'hunting_prompts': [
                {'file': 'nested.ts', 'line': 5, 'reason': 'nested obj'},
            ],
            'meta': {'a': 1},
        })
        # Texto sin code block, plano.
        mock_proc = MagicMock()
        mock_proc.stdout = f'Aquí va la respuesta:\n{valid}\nFin.'
        with patch('orchestrator.subprocess.run', return_value=mock_proc):
            result = orchestrator.call_orchestrator('snap')
        self.assertIsNotNone(result)
        self.assertEqual(result['meta'], {'a': 1})

    def test_returns_none_when_text_has_no_braces(self) -> None:
        orchestrator.ORCHESTRATOR_MODEL = 'minimax/MiniMax-M3'
        mock_proc = MagicMock()
        mock_proc.stdout = 'pura prosa sin ninguna llave'
        with patch('orchestrator.subprocess.run', return_value=mock_proc):
            self.assertIsNone(orchestrator.call_orchestrator('snap'))

    def test_returns_none_when_json_has_no_braces_after_trim(self) -> None:
        """Texto que NO contiene ni '{' ni '}' → no hay JSON que extraer → None."""
        orchestrator.ORCHESTRATOR_MODEL = 'minimax/MiniMax-M3'
        mock_proc = MagicMock()
        mock_proc.stdout = 'pura prosa sin llaves de ningún tipo, sólo palabras'
        with patch('orchestrator.subprocess.run', return_value=mock_proc):
            self.assertIsNone(orchestrator.call_orchestrator('snap'))

    def test_universaldecodeerror_falls_through_to_rfind(self) -> None:
        """Si json.loads falla Y el code-block regex no matchea, intenta rfind."""
        orchestrator.ORCHESTRATOR_MODEL = 'minimax/MiniMax-M3'
        valid = json.dumps({'hunting_prompts': [{'file': 'q.ts', 'line': 1, 'reason': 'q'}]})
        # Texto plano con JSON embebido pero SIN code block.
        mock_proc = MagicMock()
        mock_proc.stdout = f'La respuesta es {valid} y nada más.'
        with patch('orchestrator.subprocess.run', return_value=mock_proc):
            result = orchestrator.call_orchestrator('snap')
        self.assertIsNotNone(result)
        self.assertEqual(result['hunting_prompts'][0]['file'], 'q.ts')


# ---------------------------------------------------------------------------
# generate_hunting_prompts — bordes del cache
# ---------------------------------------------------------------------------


class TestGenerateHuntingPromptsEdge(unittest.TestCase):
    """Tests adicionales del comportamiento de cache / fallback."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.worktree = self.tmp.name
        self.subdir = 'apps/dashboard'
        self.root = Path(self.worktree) / self.subdir
        # Mínimo viable: un page.tsx y un actions.ts sin auth.
        _write(self.root / 'app/(panel)/a/page.tsx', '// sin dynamic\n')
        _write(self.root / 'app/(panel)/a/actions.ts', "'use server';\nexport async function f() {}\n")
        self.cache = str(Path(self.worktree) / 'cache.json')

    def test_cache_with_empty_prompts_list_regenerates(self) -> None:
        """Si la cache tiene hunting_prompts=[], el código cae al modelo."""
        Path(self.cache).write_text(
            json.dumps({'hunting_prompts': [], 'e2e_journeys': []}),
            encoding='utf-8',
        )
        os.utime(self.cache, (time.time() - 60, time.time() - 60))
        payload = {'hunting_prompts': [{'file': 'm.ts', 'line': 1, 'reason': 'r'}], 'e2e_journeys': []}
        mock_proc = MagicMock()
        mock_proc.stdout = json.dumps(payload)
        with patch('orchestrator.subprocess.run', return_value=mock_proc):
            prompts = orchestrator.generate_hunting_prompts(
                self.worktree, self.subdir, cache_path=self.cache,
            )
        # Debe haber regenerado, no devuelto la lista vacía del cache.
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0].file, 'm.ts')

    def test_cache_missing_hunting_prompts_key_regenerates(self) -> None:
        """Si el cache no tiene la clave 'hunting_prompts', regenera."""
        Path(self.cache).write_text(json.dumps({'e2e_journeys': []}), encoding='utf-8')
        os.utime(self.cache, (time.time() - 60, time.time() - 60))
        payload = {'hunting_prompts': [{'file': 'k.ts', 'line': 2, 'reason': 'k'}], 'e2e_journeys': []}
        mock_proc = MagicMock()
        mock_proc.stdout = json.dumps(payload)
        with patch('orchestrator.subprocess.run', return_value=mock_proc):
            prompts = orchestrator.generate_hunting_prompts(
                self.worktree, self.subdir, cache_path=self.cache,
            )
        self.assertEqual(prompts[0].file, 'k.ts')

    def test_cache_path_none_disables_cache(self) -> None:
        """cache_path=None → siempre regenera, nunca lee cache."""
        payload = {'hunting_prompts': [{'file': 'n.ts', 'line': 1, 'reason': 'n'}], 'e2e_journeys': []}
        mock_proc = MagicMock()
        mock_proc.stdout = json.dumps(payload)
        with patch('orchestrator.subprocess.run', return_value=mock_proc) as run:
            prompts = orchestrator.generate_hunting_prompts(
                self.worktree, self.subdir, cache_path=None,
            )
        run.assert_called_once()
        self.assertEqual(prompts[0].file, 'n.ts')

    def test_model_returns_empty_prompts_list_falls_back(self) -> None:
        """Si el modelo responde con hunting_prompts=[], se devuelve [] (no fallback)."""
        # En este caso, el código devuelve [] vacío (no llama a fallback) porque
        # prompts_raw está vacío pero result sí existe. Verificamos el comportamiento.
        payload = {'hunting_prompts': [], 'e2e_journeys': []}
        mock_proc = MagicMock()
        mock_proc.stdout = json.dumps(payload)
        with patch('orchestrator.subprocess.run', return_value=mock_proc):
            prompts = orchestrator.generate_hunting_prompts(
                self.worktree, self.subdir, cache_path=self.cache,
            )
        # Si el modelo devolvió algo (no None), no se invoca fallback.
        self.assertEqual(prompts, [])

    def test_model_with_malformed_prompt_entries_uses_defaults(self) -> None:
        """Entries con campos faltantes usan los defaults (file='', line=0).

        El código hace `p.get('file', '')`, por lo que un dict sin 'file'
        produce un HuntingPrompt con file='' — válido, no se descarta.
        """
        payload = {
            'hunting_prompts': [
                {'line': 1, 'reason': 'no file'},         # file='' por default
                {'file': 'ok.ts', 'line': 2, 'reason': 'ok'},
            ],
            'e2e_journeys': [],
        }
        mock_proc = MagicMock()
        mock_proc.stdout = json.dumps(payload)
        with patch('orchestrator.subprocess.run', return_value=mock_proc):
            prompts = orchestrator.generate_hunting_prompts(
                self.worktree, self.subdir, cache_path=self.cache,
            )
        # Ambos entries se aceptan (defaults aplicados).
        self.assertEqual(len(prompts), 2)
        self.assertEqual(prompts[0].file, '')
        self.assertEqual(prompts[1].file, 'ok.ts')

    def test_uses_why_field_as_fallback_for_reason(self) -> None:
        """Si el modelo devuelve 'why' en vez de 'reason', se usa 'why'."""
        payload = {
            'hunting_prompts': [{'file': 'a.ts', 'line': 1, 'why': 'razón alternativa'}],
            'e2e_journeys': [],
        }
        mock_proc = MagicMock()
        mock_proc.stdout = json.dumps(payload)
        with patch('orchestrator.subprocess.run', return_value=mock_proc):
            prompts = orchestrator.generate_hunting_prompts(
                self.worktree, self.subdir, cache_path=self.cache,
            )
        self.assertEqual(prompts[0].reason, 'razón alternativa')

    def test_cache_preserved_when_prompts_empty(self) -> None:
        """Si el modelo responde con hunting_prompts=[], el cache viejo NO se pisa.

        El código hace `if cache_path and prompts:` — al ser prompts=[], no escribe.
        """
        Path(self.cache).write_text(
            json.dumps({
                'hunting_prompts': [{'file': 'preserved.ts', 'line': 1, 'reason': 'p', 'context': ''}],
                'e2e_journeys': [],
            }),
            encoding='utf-8',
        )
        # Cache expirado para forzar el camino del modelo.
        os.utime(self.cache, (time.time() - 7200, time.time() - 7200))
        payload = {'hunting_prompts': [], 'e2e_journeys': []}
        mock_proc = MagicMock()
        mock_proc.stdout = json.dumps(payload)
        with patch('orchestrator.subprocess.run', return_value=mock_proc):
            orchestrator.generate_hunting_prompts(
                self.worktree, self.subdir, cache_path=self.cache,
            )
        # El cache viejo sigue intacto (no se reescribió con la lista vacía).
        with open(self.cache) as f:
            cached = json.load(f)
        self.assertEqual(cached['hunting_prompts'][0]['file'], 'preserved.ts')


# ---------------------------------------------------------------------------
# _fallback_prompts — bordes
# ---------------------------------------------------------------------------


class TestFallbackPromptsEdge(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.worktree = self.tmp.name

    def test_returns_empty_when_no_files(self) -> None:
        subdir = 'apps/empty'
        (Path(self.worktree) / subdir).mkdir(parents=True)
        self.assertEqual(orchestrator._fallback_prompts(self.worktree, subdir), [])

    def test_actions_with_both_auth_and_zod_are_skipped(self) -> None:
        subdir = 'apps/x'
        _write(
            Path(self.worktree) / subdir / 'app/(panel)/safe/actions.ts',
            "'use server';\nimport { z } from 'zod';\n"
            'import { requireRole } from "@/lib/auth";\n'
            'const s = z.object({});\nexport async function f() {\n'
            '  await requireRole("admin");\n  s.parse({});\n}\n',
        )
        prompts = orchestrator._fallback_prompts(self.worktree, subdir)
        # safe/actions.ts no debe aparecer en los prompts (tiene auth + zod).
        for p in prompts:
            self.assertNotIn('safe/actions.ts', p.file)

    def test_module_name_extraction_from_path(self) -> None:
        """app/(panel)/ventas/actions.ts → split('/')[1] = '(panel)'.

        El código actual extrae el segmento en posición 1, que para rutas
        app/(panel)/<mod>/actions.ts es el grupo de paréntesis '(panel)'.
        Verificamos que el contexto del prompt refleja esa heurística.
        """
        subdir = 'apps/x'
        _write(
            Path(self.worktree) / subdir / 'app/(panel)/ventas/actions.ts',
            "'use server';\nexport async function f() {}\n",  # sin auth, sin zod
        )
        prompts = orchestrator._fallback_prompts(self.worktree, subdir)
        self.assertGreater(len(prompts), 0)
        # El contexto debe incluir el módulo detectado ('(panel)' en este caso).
        mods = {p.context for p in prompts}
        self.assertTrue(any('(panel)' in m for m in mods))

    def test_page_without_dynamic_export_prompts(self) -> None:
        subdir = 'apps/x'
        # El check es `if 'export const dynamic' not in content`, así que el
        # contenido NO debe contener literalmente esa cadena.
        _write(
            Path(self.worktree) / subdir / 'app/(panel)/x/page.tsx',
            "// page normal sin nada\nconst a = 1;\n",
        )
        prompts = orchestrator._fallback_prompts(self.worktree, subdir)
        reasons = ' '.join(p.reason for p in prompts)
        self.assertIn('export const dynamic', reasons)


# ---------------------------------------------------------------------------
# _fallback_e2e / generate_e2e_journeys — bordes
# ---------------------------------------------------------------------------


class TestFallbackE2EEdge(unittest.TestCase):

    def test_returns_fresh_lists_each_call(self) -> None:
        """Cada llamada devuelve listas independientes (no aliasing)."""
        a = orchestrator._fallback_e2e()
        b = orchestrator._fallback_e2e()
        self.assertIsNot(a, b)
        self.assertIsNot(a[0].steps, b[0].steps)
        a[0].steps.append('mutated')
        self.assertNotIn('mutated', b[0].steps)

    def test_journey_module_includes_business_domains(self) -> None:
        journeys = orchestrator._fallback_e2e()
        modules = {j.module for j in journeys}
        self.assertIn('despacho', modules)
        self.assertIn('cartera', modules)


class TestGenerateE2EJourneysEdge(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.worktree = self.tmp.name
        self.subdir = 'apps/dashboard'
        self.root = Path(self.worktree) / self.subdir
        (self.root / 'app').mkdir(parents=True)
        self.cache = str(Path(self.worktree) / 'cache.json')

    def test_empty_worktree_returns_fallback(self) -> None:
        journeys = orchestrator.generate_e2e_journeys(
            self.worktree, 'no/existe', cache_path=self.cache,
        )
        self.assertIn('Pedido → Despacho completo', [j.name for j in journeys])

    def test_cache_with_empty_e2e_list_regenerates(self) -> None:
        """Si cache tiene e2e_journeys=[], el código cae al modelo."""
        Path(self.cache).write_text(
            json.dumps({'hunting_prompts': [], 'e2e_journeys': []}),
            encoding='utf-8',
        )
        os.utime(self.cache, (time.time() - 60, time.time() - 60))
        payload = {
            'hunting_prompts': [],
            'e2e_journeys': [
                {'name': 'regen', 'module': 'x', 'steps': ['a'], 'expected': 'ok'},
            ],
        }
        mock_proc = MagicMock()
        mock_proc.stdout = json.dumps(payload)
        with patch('orchestrator.subprocess.run', return_value=mock_proc):
            journeys = orchestrator.generate_e2e_journeys(
                self.worktree, self.subdir, cache_path=self.cache,
            )
        self.assertEqual(journeys[0].name, 'regen')

    def test_model_with_malformed_journey_entries_uses_defaults(self) -> None:
        """Entries con campos faltantes usan defaults (name=''); no se descartan."""
        payload = {
            'hunting_prompts': [],
            'e2e_journeys': [
                {'steps': ['a']},  # name='' por default
                {'name': 'valid', 'module': 'm', 'steps': ['x'], 'expected': 'ok'},
            ],
        }
        mock_proc = MagicMock()
        mock_proc.stdout = json.dumps(payload)
        with patch('orchestrator.subprocess.run', return_value=mock_proc):
            journeys = orchestrator.generate_e2e_journeys(
                self.worktree, self.subdir, cache_path=self.cache,
            )
        # Ambos se aceptan.
        self.assertEqual(len(journeys), 2)
        self.assertEqual(journeys[0].name, '')
        self.assertEqual(journeys[1].name, 'valid')

    def test_no_cache_path_always_regenerates(self) -> None:
        payload = {
            'hunting_prompts': [],
            'e2e_journeys': [
                {'name': 'always', 'module': '', 'steps': ['1'], 'expected': 'ok'},
            ],
        }
        mock_proc = MagicMock()
        mock_proc.stdout = json.dumps(payload)
        with patch('orchestrator.subprocess.run', return_value=mock_proc) as run:
            journeys = orchestrator.generate_e2e_journeys(
                self.worktree, self.subdir, cache_path=None,
            )
        run.assert_called_once()
        self.assertEqual(journeys[0].name, 'always')


# ---------------------------------------------------------------------------
# Dataclasses — bordes
# ---------------------------------------------------------------------------


class TestDataclassesEdge(unittest.TestCase):

    def test_hunting_prompt_equality(self) -> None:
        a = orchestrator.HuntingPrompt(file='a.ts', line=1, reason='r', context='c')
        b = orchestrator.HuntingPrompt(file='a.ts', line=1, reason='r', context='c')
        self.assertEqual(a, b)

    def test_hunting_prompt_inequality(self) -> None:
        a = orchestrator.HuntingPrompt(file='a.ts', line=1, reason='r', context='c')
        b = orchestrator.HuntingPrompt(file='b.ts', line=1, reason='r', context='c')
        self.assertNotEqual(a, b)

    def test_e2e_journey_equality(self) -> None:
        a = orchestrator.E2EJourney(name='n', steps=['1'], expected='e', module='m')
        b = orchestrator.E2EJourney(name='n', steps=['1'], expected='e', module='m')
        self.assertEqual(a, b)

    def test_domain_map_default_factories(self) -> None:
        """Los default_factory deben crear contenedores NUEVOS por instancia."""
        m1 = orchestrator.DomainMap()
        m2 = orchestrator.DomainMap()
        m1.modules.append('x')
        m1.state_machines['pedido'] = ['a']
        # m2 NO debe ver las mutaciones de m1.
        self.assertEqual(m2.modules, [])
        self.assertEqual(m2.state_machines, {})

    def test_domain_map_accepts_all_fields(self) -> None:
        m = orchestrator.DomainMap(
            modules=['a', 'b'],
            state_machines={'p': ['x']},
            roles=['admin'],
            auth_checks=['requireRole'],
            risky_routes=['/admin'],
            forms=['form1'],
            relations=['a→b'],
        )
        self.assertEqual(m.modules, ['a', 'b'])
        self.assertEqual(m.state_machines, {'p': ['x']})
        self.assertEqual(m.roles, ['admin'])
        self.assertEqual(m.auth_checks, ['requireRole'])
        self.assertEqual(m.risky_routes, ['/admin'])
        self.assertEqual(m.forms, ['form1'])
        self.assertEqual(m.relations, ['a→b'])

    def test_hunting_prompt_can_be_constructed_positionally(self) -> None:
        # file, line, reason son posicionalmente los primeros 3.
        p = orchestrator.HuntingPrompt('lib/x.ts', 99, 'razón')
        self.assertEqual(p.file, 'lib/x.ts')
        self.assertEqual(p.line, 99)
        self.assertEqual(p.reason, 'razón')
        self.assertEqual(p.context, '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
