#!/usr/bin/env python3
"""Edge-case tests del health loop y start_health_loop en fleet.py.

Cubre:
- _health_loop_body: probe, status write, wait, exit on shutdown
- _health_loop_body: manejo de excepciones en la iteración
- start_health_loop: spawn de Thread daemon con nombre correcto
- start_health_loop: thread ejecuta y responde a shutdown
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet  # noqa: E402
from fleet import (  # noqa: E402
    Gpu, CpuSlot, Node, Fleet,
    _health_loop_body, start_health_loop,
    write_fleet_status, FLEET_STATUS_PATH,
)


def _make_node(gpus=None, models=None, running=None, name='test',
               base_url='http://localhost:11434', is_local=True):
    return Node(
        name=name,
        base_url=base_url,
        online=True,
        gpus=gpus or [],
        cpu=CpuSlot(cores=8, ram_free_mib=16000),
        models=models or [],
        running=running or {},
        is_local=is_local,
    )


def _empty_fleet():
    return Fleet([])


class TestHealthLoopBody(unittest.TestCase):
    """_health_loop_body hace probe + write + wait hasta shutdown."""

    def setUp(self):
        self.fleet_obj = _empty_fleet()
        self.shutdown = threading.Event()

    def tearDown(self):
        self.shutdown.set()
        # Limpia el FLEET_STATUS_PATH si quedó
        for p in (FLEET_STATUS_PATH, FLEET_STATUS_PATH + '.tmp'):
            try:
                os.unlink(p)
            except OSError:
                pass

    def test_calls_discover_models_running_and_gpus(self):
        """El body llama a discover_models, discover_running, discover_local_gpus."""
        node = _make_node(name='n1', base_url='http://x:11434', is_local=True)
        self.fleet_obj = Fleet([node])

        with patch('fleet.discover_models', return_value=['m1']) as m_models, \
             patch('fleet.discover_running', return_value={'m1': 100}) as m_running, \
             patch('fleet.discover_local_gpus', return_value=[]) as m_gpus, \
             patch('fleet.write_fleet_status') as m_write, \
             patch('fleet.HEALTH_INTERVAL_SECS', 0.05):
            # Shutdown tras 80ms — garantiza al menos una iteración completa
            def trigger():
                time.sleep(0.08)
                self.shutdown.set()
            t = threading.Thread(target=trigger, daemon=True)
            t.start()
            _health_loop_body(self.fleet_obj, self.shutdown)
            t.join(timeout=2)

        m_models.assert_called_with('http://x:11434')
        m_running.assert_called_with('http://x:11434')
        m_gpus.assert_called()  # is_local=True
        m_write.assert_called()

    def test_writes_status_at_least_once(self):
        """El body llama write_fleet_status al menos una vez por iteración."""
        node = _make_node(name='n1', base_url='http://x:11434', is_local=False)
        self.fleet_obj = Fleet([node])

        with patch('fleet.discover_models', return_value=[]), \
             patch('fleet.discover_running', return_value={}), \
             patch('fleet.write_fleet_status') as m_write, \
             patch('fleet.HEALTH_INTERVAL_SECS', 0.05):
            def trigger():
                time.sleep(0.08)
                self.shutdown.set()
            t = threading.Thread(target=trigger, daemon=True)
            t.start()
            _health_loop_body(self.fleet_obj, self.shutdown)
            t.join(timeout=2)

        self.assertGreaterEqual(m_write.call_count, 1)

    def test_discovers_and_writes_then_exits_on_shutdown(self):
        """Happy path: probe, write, wait, exit cuando shutdown se setea durante el wait."""
        node = _make_node(name='n1', base_url='http://x:11434', is_local=False)
        self.fleet_obj = Fleet([node])
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, 'fleet.json')

        with patch('fleet.discover_models', return_value=['m1']), \
             patch('fleet.discover_running', return_value={}), \
             patch('fleet.HEALTH_INTERVAL_SECS', 0.05):
            # Shutdown tras 100ms — el body debe ejecutar al menos una iteración y salir
            def trigger():
                time.sleep(0.1)
                self.shutdown.set()
            t = threading.Thread(target=trigger, daemon=True)
            t.start()
            _health_loop_body(self.fleet_obj, self.shutdown)
            t.join(timeout=2)

        # El body escribió a FLEET_STATUS_PATH (default)
        # El fleet_obj debe tener los modelos actualizados
        self.assertEqual(self.fleet_obj.nodes[0].models, ['m1'])
        self.assertTrue(self.fleet_obj.nodes[0].online)

        for p in (path, FLEET_STATUS_PATH, FLEET_STATUS_PATH + '.tmp'):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass

    def test_catches_exception_in_body_continues(self):
        """Si una iteración del body lanza, el loop continúa y eventualmente sale."""
        node = _make_node(name='n1', base_url='http://x:11434', is_local=False)
        self.fleet_obj = Fleet([node])

        call_count = {'n': 0}

        def fake_models(_url):
            call_count['n'] += 1
            if call_count['n'] == 1:
                raise RuntimeError('boom')
            return []

        with patch('fleet.discover_models', side_effect=fake_models), \
             patch('fleet.discover_running', return_value={}), \
             patch('fleet.write_fleet_status'), \
             patch('fleet.HEALTH_INTERVAL_SECS', 0.01):
            # Shutdown tras 80ms
            def trigger():
                time.sleep(0.08)
                self.shutdown.set()
            t = threading.Thread(target=trigger, daemon=True)
            t.start()
            _health_loop_body(self.fleet_obj, self.shutdown)
            t.join(timeout=2)

        # Debe haber sido llamada más de una vez (no murió en la 1ra)
        self.assertGreater(call_count['n'], 1,
                           "El loop debe continuar tras una excepción")


class TestStartHealthLoop(unittest.TestCase):
    """start_health_loop spawna un Thread daemon con la lógica del health loop."""

    def setUp(self):
        self.fleet_obj = _empty_fleet()
        self.shutdown = threading.Event()
        self.threads = []

    def tearDown(self):
        self.shutdown.set()
        for t in self.threads:
            t.join(timeout=3)
            self.assertFalse(t.is_alive(), f"Thread {t.name} sigue vivo tras join")

        for p in (FLEET_STATUS_PATH, FLEET_STATUS_PATH + '.tmp'):
            try:
                os.unlink(p)
            except OSError:
                pass

    def test_returns_thread(self):
        """start_health_loop devuelve un objeto threading.Thread."""
        with patch('fleet.HEALTH_INTERVAL_SECS', 0.05):
            t = start_health_loop(self.fleet_obj, self.shutdown)
        self.threads.append(t)
        self.assertIsInstance(t, threading.Thread)

    def test_thread_is_daemon(self):
        with patch('fleet.HEALTH_INTERVAL_SECS', 0.05):
            t = start_health_loop(self.fleet_obj, self.shutdown)
        self.threads.append(t)
        self.assertTrue(t.daemon, "El thread del health loop debe ser daemon")

    def test_thread_name(self):
        with patch('fleet.HEALTH_INTERVAL_SECS', 0.05):
            t = start_health_loop(self.fleet_obj, self.shutdown)
        self.threads.append(t)
        self.assertEqual(t.name, 'fleet-health-loop')

    def test_thread_actually_runs_probe_and_writes(self):
        """El thread hace probe real y write_fleet_status."""
        node = _make_node(name='n1', base_url='http://x:11434', is_local=False)
        self.fleet_obj = Fleet([node])

        with patch('fleet.discover_models', return_value=['m1', 'm2']) as m_models, \
             patch('fleet.discover_running', return_value={}), \
             patch('fleet.HEALTH_INTERVAL_SECS', 0.05):
            t = start_health_loop(self.fleet_obj, self.shutdown)
            self.threads.append(t)
            # Espera a que la 1ra iteración termine
            time.sleep(0.2)
            self.shutdown.set()
            t.join(timeout=3)

        # El thread corrió al menos una iteración
        m_models.assert_called()
        # El fleet fue actualizado
        self.assertEqual(self.fleet_obj.nodes[0].models, ['m1', 'm2'])
        self.assertTrue(self.fleet_obj.nodes[0].online)
        # El status file debe existir
        self.assertTrue(os.path.exists(FLEET_STATUS_PATH),
                        f"start_health_loop debe escribir {FLEET_STATUS_PATH}")

    def test_thread_exits_on_shutdown(self):
        """El thread termina cuando shutdown_event se setea."""
        with patch('fleet.HEALTH_INTERVAL_SECS', 5.0):  # interval largo
            t = start_health_loop(self.fleet_obj, self.shutdown)
        self.threads.append(t)
        time.sleep(0.1)  # deja que arranque
        self.assertTrue(t.is_alive(), "El thread debe estar vivo antes de shutdown")
        self.shutdown.set()
        t.join(timeout=3)
        self.assertFalse(t.is_alive(), "El thread debe terminar tras shutdown")


if __name__ == '__main__':
    unittest.main()
