#!/usr/bin/env python3
"""E2E pipeline tests — happy path: _map_e2e_route_to_file, _dev_server_alive,
_dev_server_up, _run_e2e_route base behavior, e2e_producer basics."""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402


class _WTFixture(unittest.TestCase):
    """Set up an isolated WT/SUBDIR tree with `app/(panel)/<x>/page.tsx` files."""

    PANEL_ROUTES = ('clientes', 'inventario', 'cobros')

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        panel = self.tmpdir / 'app' / '(panel)'
        for r in self.PANEL_ROUTES:
            (panel / r).mkdir(parents=True, exist_ok=True)
            (panel / r / 'page.tsx').write_text(f'// {r}\n')
        # app/page.tsx for the empty/home route mapping.
        (self.tmpdir / 'app' / 'page.tsx').parent.mkdir(parents=True, exist_ok=True)
        (self.tmpdir / 'app' / 'page.tsx').write_text('// home\n')
        # A non-panel route: app/admin-tools/page.tsx (no parentheses).
        (self.tmpdir / 'app' / 'admin-tools').mkdir(parents=True, exist_ok=True)
        (self.tmpdir / 'app' / 'admin-tools' / 'page.tsx').write_text('// admin-tools\n')
        # A pages/ legacy route.
        (self.tmpdir / 'pages').mkdir(parents=True, exist_ok=True)
        (self.tmpdir / 'pages' / 'legacy.tsx').write_text('// legacy\n')
        (self.tmpdir / 'pages' / 'reports').mkdir(parents=True, exist_ok=True)
        (self.tmpdir / 'pages' / 'reports' / 'index.tsx').write_text('// reports\n')

        self._saved_wt = harness.WT
        self._saved_subdir = harness.SUBDIR
        self._saved_wt_subdir = harness.WT_SUBDIR
        self._saved_storage = harness.STORAGE
        self._saved_dev_url = harness.DEV_URL
        harness.WT = self.tmpdir
        harness.SUBDIR = '.'
        harness.WT_SUBDIR = self.tmpdir

    def tearDown(self):
        harness.WT = self._saved_wt
        harness.SUBDIR = self._saved_subdir
        harness.WT_SUBDIR = self._saved_wt_subdir
        harness.STORAGE = self._saved_storage
        harness.DEV_URL = self._saved_dev_url
        self._tmp.cleanup()


class TestMapE2eRouteToFile(_WTFixture):
    """_map_e2e_route_to_file: candidate resolution from /<route> to worktree path."""

    def test_panel_route_resolves(self):
        """'/clientes' → 'app/(panel)/clientes/page.tsx' (first candidate wins)."""
        result = harness._map_e2e_route_to_file('/clientes')
        self.assertEqual(result, 'app/(panel)/clientes/page.tsx')

    def test_empty_route_resolves_to_home(self):
        """'' (empty) → 'app/page.tsx' (no candidates checked, home shortcut)."""
        result = harness._map_e2e_route_to_file('')
        self.assertEqual(result, 'app/page.tsx')

    def test_root_slash_route_resolves_to_home(self):
        """'/' (just slash) → 'app/page.tsx' (stripped to empty → home)."""
        result = harness._map_e2e_route_to_file('/')
        self.assertEqual(result, 'app/page.tsx')

    def test_unknown_route_returns_none(self):
        """'/inbox' not in any candidate → None."""
        result = harness._map_e2e_route_to_file('/inbox')
        self.assertIsNone(result)

    def test_fallback_to_app_non_panel(self):
        """'/admin-tools' → 'app/admin-tools/page.tsx' (panel miss, app/<route> hit)."""
        result = harness._map_e2e_route_to_file('/admin-tools')
        self.assertEqual(result, 'app/admin-tools/page.tsx')

    def test_fallback_to_pages_dot_tsx(self):
        """'/legacy' lives only in pages/ legacy form → 'pages/legacy.tsx'."""
        # Remove panel + non-panel candidates so pages/<route>.tsx wins.
        panel_legacy = self.tmpdir / 'app' / '(panel)' / 'legacy'
        if panel_legacy.exists():
            (self.tmpdir / 'app' / '(panel)' / 'legacy').rmdir()
        app_legacy = self.tmpdir / 'app' / 'legacy'
        if app_legacy.exists():
            (self.tmpdir / 'app' / 'legacy').rmdir()
        result = harness._map_e2e_route_to_file('/legacy')
        self.assertEqual(result, 'pages/legacy.tsx')

    def test_fallback_to_pages_index_tsx(self):
        """'/reports' lives in pages/reports/index.tsx → 'pages/reports/index.tsx'."""
        result = harness._map_e2e_route_to_file('/reports')
        self.assertEqual(result, 'pages/reports/index.tsx')

    def test_case_insensitive_lowering(self):
        """'/CLIENTES' lowered to '/clientes' → panel hit."""
        result = harness._map_e2e_route_to_file('/CLIENTES')
        self.assertEqual(result, 'app/(panel)/clientes/page.tsx')

    def test_strips_surrounding_slashes(self):
        """'/cobros/' (trailing slash) → same as '/cobros'."""
        result = harness._map_e2e_route_to_file('/cobros/')
        self.assertEqual(result, 'app/(panel)/cobros/page.tsx')


class TestDevServerAlive(_WTFixture):
    """_dev_server_alive: HEAD probe; any <600 status is alive, exception is dead."""

    @staticmethod
    def _http_response(status: int) -> MagicMock:
        resp = MagicMock()
        resp.status = status
        resp.__enter__ = lambda self_: self_
        resp.__exit__ = lambda self_, *a: False
        return resp

    def test_status_200_alive(self):
        """200 → True (200 < 600)."""
        with patch('harness.urllib.request.urlopen') as uo:
            uo.return_value = self._http_response(200)
            self.assertTrue(harness._dev_server_alive())

    def test_status_500_alive(self):
        """500 → True (500 < 600). Live is "responds", not "200 OK"."""
        with patch('harness.urllib.request.urlopen') as uo:
            uo.return_value = self._http_response(500)
            self.assertTrue(harness._dev_server_alive())

    def test_status_599_alive(self):
        """599 → True (599 < 600). Last "live" status code."""
        with patch('harness.urllib.request.urlopen') as uo:
            uo.return_value = self._http_response(599)
            self.assertTrue(harness._dev_server_alive())

    def test_status_600_dead(self):
        """600 → False (600 is NOT < 600)."""
        with patch('harness.urllib.request.urlopen') as uo:
            uo.return_value = self._http_response(600)
            self.assertFalse(harness._dev_server_alive())

    def test_status_700_dead(self):
        """700 → False (way above threshold)."""
        with patch('harness.urllib.request.urlopen') as uo:
            uo.return_value = self._http_response(700)
            self.assertFalse(harness._dev_server_alive())

    def test_connection_error_dead(self):
        """URLError (connection refused, DNS) → False."""
        with patch('harness.urllib.request.urlopen',
                   side_effect=urllib.error.URLError('Connection refused')):
            self.assertFalse(harness._dev_server_alive())

    def test_timeout_dead(self):
        """socket.timeout → False (dev server hung)."""
        with patch('harness.urllib.request.urlopen', side_effect=TimeoutError('timed out')):
            self.assertFalse(harness._dev_server_alive())

    def test_generic_exception_dead(self):
        """Any other exception → False (defensive default)."""
        with patch('harness.urllib.request.urlopen', side_effect=RuntimeError('boom')):
            self.assertFalse(harness._dev_server_alive())


class TestDevServerUp(_WTFixture):
    """_dev_server_up: GET '/' probe; <500 is up, ≥500 is down, exception is down."""

    @staticmethod
    def _http_response(status: int) -> MagicMock:
        resp = MagicMock()
        resp.status = status
        resp.__enter__ = lambda self_: self_
        resp.__exit__ = lambda self_, *a: False
        return resp

    def test_status_200_up(self):
        """200 → True."""
        with patch('harness.urllib.request.urlopen') as uo:
            uo.return_value = self._http_response(200)
            self.assertTrue(harness._dev_server_up())

    def test_status_404_up(self):
        """404 → True (server responds, page just doesn't exist)."""
        with patch('harness.urllib.request.urlopen') as uo:
            uo.return_value = self._http_response(404)
            self.assertTrue(harness._dev_server_up())

    def test_status_499_up(self):
        """499 → True (right at the boundary, 499 < 500)."""
        with patch('harness.urllib.request.urlopen') as uo:
            uo.return_value = self._http_response(499)
            self.assertTrue(harness._dev_server_up())

    def test_status_500_down(self):
        """500 → False (boundary, 500 NOT < 500)."""
        with patch('harness.urllib.request.urlopen') as uo:
            uo.return_value = self._http_response(500)
            self.assertFalse(harness._dev_server_up())

    def test_status_502_down(self):
        """502 → False (Bad Gateway, common when dev server crashes)."""
        with patch('harness.urllib.request.urlopen') as uo:
            uo.return_value = self._http_response(502)
            self.assertFalse(harness._dev_server_up())

    def test_connection_error_down(self):
        """URLError → False."""
        with patch('harness.urllib.request.urlopen',
                   side_effect=urllib.error.URLError('refused')):
            self.assertFalse(harness._dev_server_up())

    def test_timeout_down(self):
        """TimeoutError → False."""
        with patch('harness.urllib.request.urlopen', side_effect=TimeoutError('slow')):
            self.assertFalse(harness._dev_server_up())


class TestRunE2eRoute(_WTFixture):
    """_run_e2e_route: subprocess orchestration, JSON output contract, error filtering."""

    def _ok_subprocess(self, payload: dict) -> MagicMock:
        m = MagicMock()
        m.stdout = json.dumps(payload)
        m.stderr = ''
        m.returncode = 0
        return m

    def test_clean_run_returns_true_empty(self):
        """ok=True, no errors → (True, [])."""
        with patch('harness.subprocess.run',
                   return_value=self._ok_subprocess({'ok': True, 'errors': []})):
            ran, fatal = harness._run_e2e_route('/clientes')
        self.assertTrue(ran)
        self.assertEqual(fatal, [])

    def test_filters_fatal_kinds(self):
        """Errors with kinds in {http, pageerror, errorUI, console} are surfaced as fatal."""
        payload = {
            'ok': False,
            'errors': [
                {'kind': 'http', 'detail': '500 from /api/x'},
                {'kind': 'pageerror', 'detail': 'TypeError'},
                {'kind': 'errorUI', 'detail': 'red error boundary'},
                {'kind': 'console', 'detail': 'warn: missing dep'},
                {'kind': 'metrics', 'detail': 'lcp 5.2s'},  # not fatal
                {'kind': 'navigation', 'detail': 'slow load'},  # not fatal
            ],
        }
        with patch('harness.subprocess.run',
                   return_value=self._ok_subprocess(payload)):
            ran, fatal = harness._run_e2e_route('/clientes')
        self.assertTrue(ran)
        self.assertEqual(len(fatal), 4)
        kinds = [e['kind'] for e in fatal]
        self.assertEqual(kinds, ['http', 'pageerror', 'errorUI', 'console'])

    def test_fatal_in_output_returns_false(self):
        """out.get('fatal') present → (False, []) — runner aborted, can't trust errors."""
        payload = {'fatal': 'Browser crashed: out of memory'}
        with patch('harness.subprocess.run',
                   return_value=self._ok_subprocess(payload)):
            ran, fatal = harness._run_e2e_route('/clientes')
        self.assertFalse(ran)
        self.assertEqual(fatal, [])

    def test_subprocess_exception_returns_false(self):
        """subprocess.run raises (e.g. node missing) → (False, [])."""
        with patch('harness.subprocess.run',
                   side_effect=FileNotFoundError('node not found')):
            ran, fatal = harness._run_e2e_route('/clientes')
        self.assertFalse(ran)
        self.assertEqual(fatal, [])

    def test_invalid_json_returns_false(self):
        """subprocess returns garbage that's not JSON → (False, [])."""
        m = MagicMock()
        m.stdout = 'definitely not json {'
        with patch('harness.subprocess.run', return_value=m):
            ran, fatal = harness._run_e2e_route('/clientes')
        self.assertFalse(ran)
        self.assertEqual(fatal, [])

    def test_storage_included_in_scenario_when_set(self):
        """STORAGE non-empty → scenario includes 'storageState' (auth reuse)."""
        harness.STORAGE = '/tmp/storage.json'
        captured = {}

        def fake_run(cmd, **kwargs):
            captured['input'] = kwargs.get('input')
            m = MagicMock()
            m.stdout = json.dumps({'ok': True, 'errors': []})
            return m

        with patch('harness.subprocess.run', side_effect=fake_run):
            harness._run_e2e_route('/clientes')
        scn = json.loads(captured['input'])
        self.assertIn('storageState', scn)
        self.assertEqual(scn['storageState'], '/tmp/storage.json')

    def test_storage_absent_when_empty(self):
        """STORAGE empty → scenario has no 'storageState' (demo mode)."""
        harness.STORAGE = ''
        captured = {}

        def fake_run(cmd, **kwargs):
            captured['input'] = kwargs.get('input')
            m = MagicMock()
            m.stdout = json.dumps({'ok': True, 'errors': []})
            return m

        with patch('harness.subprocess.run', side_effect=fake_run):
            harness._run_e2e_route('/clientes')
        scn = json.loads(captured['input'])
        self.assertNotIn('storageState', scn)
        self.assertEqual(scn['route'], '/clientes')
        self.assertEqual(scn['maxActions'], 12)
        self.assertEqual(scn['budgetMs'], 45000)

    def test_timeout_raises_returns_false(self):
        """Subprocess.TimeoutExpired when timeout arg exceeded → (False, [])."""
        with patch('harness.subprocess.run',
                   side_effect=subprocess.TimeoutExpired(cmd='node', timeout=0.001)):
            ran, fatal = harness._run_e2e_route('/clientes', timeout=0.001)
        self.assertFalse(ran)
        self.assertEqual(fatal, [])

    def test_empty_errors_list_returns_empty_fatal(self):
        """'errors' key absent from output → fatal defaults to empty."""
        with patch('harness.subprocess.run',
                   return_value=self._ok_subprocess({'ok': True})):
            ran, fatal = harness._run_e2e_route('/clientes')
        self.assertTrue(ran)
        self.assertEqual(fatal, [])

    def test_error_without_kind_not_in_fatal(self):
        """Error entries missing 'kind' are NOT in fatal (defensive filter)."""
        payload = {'ok': False, 'errors': [
            {'detail': 'orphan error'},  # no kind
            {'kind': 'http', 'detail': '500'},
        ]}
        with patch('harness.subprocess.run',
                   return_value=self._ok_subprocess(payload)):
            ran, fatal = harness._run_e2e_route('/clientes')
        self.assertTrue(ran)
        self.assertEqual(len(fatal), 1)
        self.assertEqual(fatal[0]['detail'], '500')


class TestE2eProducerEnqueues(_WTFixture):
    """e2e_producer: pulls from discover_zones + journeys + auth, dedup, stop."""

    PANEL_ROUTES = ('clientes', 'inventario', 'cobros')

    def setUp(self):
        super().setUp()
        # Clear module-level state.
        while not harness.e2e_taskq.empty():
            try:
                harness.e2e_taskq.get_nowait()
            except queue.Empty:
                break
        self._saved_journeys = list(harness._current_e2e_journeys)
        with harness._ORCH_LOCK:
            harness._current_e2e_journeys = []

    def tearDown(self):
        # Drain whatever the producer enqueued.
        while not harness.e2e_taskq.empty():
            try:
                harness.e2e_taskq.get_nowait()
            except queue.Empty:
                break
        with harness._ORCH_LOCK:
            harness._current_e2e_journeys = self._saved_journeys
        super().tearDown()

    def _drain(self) -> list:
        items = []
        while not harness.e2e_taskq.empty():
            try:
                items.append(harness.e2e_taskq.get_nowait())
            except queue.Empty:
                break
        return items

    def test_discovers_panel_routes(self):
        """Routes come from discover_zones() — one per panel/<x>."""
        stop = threading.Event()
        t = threading.Thread(target=harness.e2e_producer, args=(stop,), daemon=True)
        t.start()
        # Allow one full loop iteration to enqueue.
        time.sleep(0.1)
        stop.set()
        t.join(timeout=5)
        items = self._drain()
        routes = {item[0] for item in items}
        # 3 panel routes + 4 auth routes.
        self.assertIn('/clientes', routes)
        self.assertIn('/inventario', routes)
        self.assertIn('/cobros', routes)

    def test_includes_orchestrator_journey_routes(self):
        """Routes come from _current_e2e_journeys steps starting with '/'."""
        journey = SimpleNamespace(
            module='cobros',
            steps=['/cobros', '/cobros/nuevo', '/cobros/123'],
        )
        with harness._ORCH_LOCK:
            harness._current_e2e_journeys = [journey]
        stop = threading.Event()
        t = threading.Thread(target=harness.e2e_producer, args=(stop,), daemon=True)
        t.start()
        time.sleep(0.1)
        stop.set()
        t.join(timeout=5)
        items = self._drain()
        routes = {item[0] for item in items}
        # The journey provides /cobros/nuevo and /cobros/123 (NOT /cobros, which
        # also comes from discover_zones, dedup keeps one entry).
        self.assertIn('/cobros/nuevo', routes)
        self.assertIn('/cobros/123', routes)

    def test_includes_auth_routes(self):
        """Always-on auth routes: /sign-in, /sign-up, /inicio, /'."""
        stop = threading.Event()
        t = threading.Thread(target=harness.e2e_producer, args=(stop,), daemon=True)
        t.start()
        time.sleep(0.1)
        stop.set()
        t.join(timeout=5)
        items = self._drain()
        routes = {item[0] for item in items}
        for required in ('/sign-in', '/sign-up', '/inicio', '/'):
            self.assertIn(required, routes)

    def test_stops_when_stop_event_set(self):
        """Stop event is honored: thread exits cleanly."""
        stop = threading.Event()
        t = threading.Thread(target=harness.e2e_producer, args=(stop,), daemon=True)
        t.start()
        # Set immediately; producer should exit on next loop check.
        stop.set()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), 'producer did not stop on stop_event')

    def test_deduplicates_across_iterations(self):
        """The same (route, module, depth) is only enqueued once across loops."""
        stop = threading.Event()
        t = threading.Thread(target=harness.e2e_producer, args=(stop,), daemon=True)
        t.start()
        # Let the producer run for a bit, but don't drain the queue.
        time.sleep(0.5)
        stop.set()
        t.join(timeout=5)
        items = self._drain()
        keys = [(it[0], it[1], it[2]) for it in items]
        # All keys unique — produced set is the dedup mechanism.
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
