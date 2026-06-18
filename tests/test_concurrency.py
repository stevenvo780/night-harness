#!/usr/bin/env python3
"""Concurrency stress tests for harness.py + quota.py.

Verifica que bajo carga concurrente intensa:
  · Contadores (STATS, call_log, by_model_pct) no pierden datos.
  · Locks (file_lock, ollama_sem, _HEALTH_LOCK, STATS_LOCK, THREAD_STATES_LOCK,
    _STATE.lock) no generan deadlocks ni races.
  · THREAD_STATES es seguro bajo add/update concurrentes.
  · El estado de salud / cuarentena queda consistente.
  · MEM.record_bug no duplica bugs ni pierde times_confirmed.
  · Las colas suspectq / bugq mueven todos los items sin pérdida.
  · El lock por archivo se libera en excepción.
  · Un workload mixto (hunters + filtros + jueces) no se atasca.

Correr:
    cd /workspace/night-harness && python3 -m unittest tests.test_concurrency -v
"""
from __future__ import annotations

import concurrent.futures
import os
import queue
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402
import memory  # noqa: E402
import quota  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_harness_state() -> None:
    """Limpia el estado global del harness entre tests para aislamiento."""
    with harness.STATS_LOCK:
        for k in harness.STATS:
            if k == 'started_at':
                continue
            harness.STATS[k] = 0
    with harness._HEALTH_LOCK:
        harness._HEALTH.clear()
    with harness._FILE_LOCKS_GUARD:
        harness._FILE_LOCKS.clear()
    with harness._OLLAMA_SEM_GUARD:
        harness._OLLAMA_SEMAPHORES.clear()
    with harness.THREAD_STATES_LOCK:
        harness.THREAD_STATES.clear()


def _reset_quota_state() -> None:
    with quota._STATE.lock:
        quota._STATE.by_model_pct.clear()
        quota._STATE.call_log.clear()
        quota._STATE.timestamp = 0.0


def _drain_queue(q: queue.Queue) -> list:
    """Saca todos los items de una cola hasta vaciarla."""
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            break
    return out


def _run_with_timeout(fn, timeout: float) -> bool:
    """Ejecuta `fn()` en un thread; True si termina antes de `timeout`."""
    done = threading.Event()
    err = []

    def wrapper():
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            err.append(e)
        finally:
            done.set()

    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    finished = done.wait(timeout=timeout)
    if not finished:
        return False
    if err:
        raise err[0]
    return True


# ---------------------------------------------------------------------------
# 1) STATS counter
# ---------------------------------------------------------------------------
class TestStatsCounter(unittest.TestCase):
    """STATS['rounds'] += N con N hilos no debe perder updates si está protegido."""

    def setUp(self):
        with harness.STATS_LOCK:
            for k in harness.STATS:
                if k == 'started_at':
                    continue
                harness.STATS[k] = 0

    def test_stats_counter_no_loss(self):
        """100 threads × 1 increment con lock → final == 100 (sin pérdida)."""
        n = 100

        def worker(_i):
            with harness.STATS_LOCK:
                harness.STATS['rounds'] += 1

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(worker, range(n)))

        self.assertEqual(harness.STATS['rounds'], n,
                         f"STATS['rounds'] perdió updates: {harness.STATS['rounds']}/{n}")

    def test_stats_counter_with_lock_explicit(self):
        """Variante: el caller protege con `with STATS_LOCK:` y cuenta sospechosos."""
        n = 100

        def worker(_i):
            with harness.STATS_LOCK:
                harness.STATS['suspects_produced'] += 1
                # Anidamiento de operaciones (lectura + escritura) protegido.
                _ = harness.STATS['suspects_produced']
                harness.STATS['suspects_produced'] += 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(worker, range(n)))

        self.assertEqual(harness.STATS['suspects_produced'], n)


# ---------------------------------------------------------------------------
# 2) THREAD_STATES
# ---------------------------------------------------------------------------
class TestThreadStates(unittest.TestCase):
    """THREAD_STATES debe aceptar updates/adds concurrentes sin corromperse."""

    def setUp(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def tearDown(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def test_thread_states_concurrent_add(self):
        """50 threads añaden nombres distintos → THREAD_STATES tiene 50 entradas."""
        n = 50
        barrier = threading.Barrier(n)

        def worker(i):
            barrier.wait()
            harness.update_thread_state(f'worker-{i}', kind='hunter',
                                        model=f'model-{i}', status='busy')

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(worker, range(n)))

        self.assertEqual(len(harness.THREAD_STATES), n,
                         f'perdidas: {n - len(harness.THREAD_STATES)}')
        for i in range(n):
            self.assertIn(f'worker-{i}', harness.THREAD_STATES)

    def test_thread_states_concurrent_update(self):
        """50 threads actualizan el mismo name → no crashea, last_update reciente."""
        n = 50
        barrier = threading.Barrier(n)
        errors = []
        t_before = time.time()

        def worker(_i):
            try:
                barrier.wait()
                for _ in range(20):
                    harness.update_thread_state(
                        'shared-name', kind='filter',
                        model='rotating', status='busy', calls=1,
                    )
                    time.sleep(0.001)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(worker, range(n)))

        self.assertEqual(errors, [], f'crashes: {errors}')
        with harness.THREAD_STATES_LOCK:
            self.assertIn('shared-name', harness.THREAD_STATES)
            entry = harness.THREAD_STATES['shared-name']
            self.assertGreaterEqual(entry['last_update'], t_before)
            self.assertEqual(entry['model'], 'rotating')

    def test_update_thread_state_idempotent(self):
        """1000 calls con mismo name+kwargs → una sola entrada, no growth."""
        n = 1000
        barrier = threading.Barrier(min(n, 100))

        def worker(_i):
            barrier.wait()
            harness.update_thread_state(
                'idem-name', kind='judge', model='judge-A',
                status='idle', location='cloud', node='cloud',
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=100) as ex:
            list(ex.map(worker, range(n)))

        with harness.THREAD_STATES_LOCK:
            self.assertEqual(len(harness.THREAD_STATES), 1)
            entry = harness.THREAD_STATES['idem-name']
            self.assertEqual(entry['model'], 'judge-A')
            self.assertEqual(entry['kind'], 'judge')

    def test_get_thread_states_safe_under_writes(self):
        """get_thread_states() mientras otros hilos escriben no debe crashear."""
        stop = threading.Event()
        errors = []
        n_writers = 20

        def writer(i):
            try:
                while not stop.is_set():
                    harness.update_thread_state(
                        f'w-{i}', kind='cloud', model='m', calls=1,
                    )
                    time.sleep(0.0005)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def reader():
            try:
                for _ in range(100):
                    snap = harness.get_thread_states()
                    self.assertIsInstance(snap, list)
                    time.sleep(0.001)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        writers = [threading.Thread(target=writer, args=(i,))
                   for i in range(n_writers)]
        for t in writers:
            t.start()
        readers = [threading.Thread(target=reader) for _ in range(4)]
        for t in readers:
            t.start()

        for t in readers:
            t.join(timeout=5)
        stop.set()
        for t in writers:
            t.join(timeout=2)

        self.assertEqual(errors, [], f'errores en readers/writers: {errors[:3]}')


# ---------------------------------------------------------------------------
# 3) _FILE_LOCKS
# ---------------------------------------------------------------------------
class TestFileLocks(unittest.TestCase):
    """_file_lock(file) debe serializar accesos al mismo archivo."""

    def setUp(self):
        with harness._FILE_LOCKS_GUARD:
            harness._FILE_LOCKS.clear()

    def tearDown(self):
        with harness._FILE_LOCKS_GUARD:
            harness._FILE_LOCKS.clear()

    def test_file_lock_serializes(self):
        """2 threads al mismo archivo: el segundo espera al primero."""
        lock = harness._file_lock('app/(panel)/clientes/page.tsx')
        order = []
        order_lock = threading.Lock()

        def thread_a():
            if not lock.acquire(blocking=True, timeout=2):
                with order_lock:
                    order.append('a-timeout')
                return
            try:
                with order_lock:
                    order.append('a-in')
                time.sleep(0.1)
            finally:
                lock.release()
                with order_lock:
                    order.append('a-out')

        def thread_b():
            time.sleep(0.02)  # asegura que a arranca primero
            if not lock.acquire(blocking=True, timeout=2):
                with order_lock:
                    order.append('b-timeout')
                return
            try:
                with order_lock:
                    order.append('b-in')
            finally:
                lock.release()
                with order_lock:
                    order.append('b-out')

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start(); tb.start()
        ta.join(timeout=3); tb.join(timeout=3)

        # a-in debe preceder a a-out, que debe preceder a b-in (serialización).
        self.assertIn('a-in', order)
        self.assertIn('a-out', order)
        self.assertIn('b-in', order)
        i_a_in = order.index('a-in')
        i_a_out = order.index('a-out')
        i_b_in = order.index('b-in')
        self.assertLess(i_a_in, i_a_out)
        self.assertLess(i_a_out, i_b_in,
                        f'b entró antes de que a soltara: order={order}')
        self.assertFalse(lock.locked())

    def test_file_lock_distinct_files(self):
        """2 threads a archivos distintos: corren en paralelo (no contención)."""
        lock_a = harness._file_lock('app/(panel)/a/page.tsx')
        lock_b = harness._file_lock('app/(panel)/b/page.tsx')
        self.assertIsNot(lock_a, lock_b,
                         'archivos distintos deben tener locks distintos')

        # Adquirir ambos en threads paralelos no debe bloquearse.
        acquired = []
        acq_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def hold(lock, name):
            barrier.wait()
            self.assertTrue(lock.acquire(blocking=True, timeout=2))
            try:
                with acq_lock:
                    acquired.append(name)
                time.sleep(0.05)
            finally:
                lock.release()

        ta = threading.Thread(target=hold, args=(lock_a, 'a'))
        tb = threading.Thread(target=hold, args=(lock_b, 'b'))
        t0 = time.time()
        ta.start(); tb.start()
        ta.join(timeout=3); tb.join(timeout=3)
        elapsed = time.time() - t0

        self.assertEqual(sorted(acquired), ['a', 'b'])
        # Si fueran serializados serían ~0.1s. En paralelo ~0.05s.
        # Damos margen generoso.
        self.assertLess(elapsed, 0.2,
                        f'demasiado lento ({elapsed:.3f}s); parece serializado')

    def test_file_lock_releases_on_exception(self):
        """Si el thread que tiene el lock lanza una excepción, el lock se libera."""
        lock = harness._file_lock('app/(panel)/x/page.tsx')
        errors = []

        def holder():
            try:
                self.assertTrue(lock.acquire(blocking=True, timeout=1))
                try:
                    time.sleep(0.05)
                    raise RuntimeError('boom inside critical section')
                finally:
                    lock.release()
            except RuntimeError:
                pass  # esperado

        def acquirer():
            try:
                time.sleep(0.02)
                self.assertTrue(
                    lock.acquire(blocking=True, timeout=2),
                    'lock no se liberó tras excepción del holder',
                )
                lock.release()
            except AssertionError as e:
                errors.append(str(e))

        t1 = threading.Thread(target=holder)
        t2 = threading.Thread(target=acquirer)
        t1.start(); t2.start()
        t1.join(timeout=3); t2.join(timeout=3)

        self.assertEqual(errors, [], errors)
        self.assertFalse(lock.locked())

    def test_file_lock_lookup_concurrent(self):
        """_file_lock() para el mismo archivo desde N threads devuelve el mismo Lock."""
        n = 50
        barrier = threading.Barrier(n)
        seen = []
        seen_lock = threading.Lock()

        def worker(_i):
            barrier.wait()
            lk = harness._file_lock('app/(panel)/shared/page.tsx')
            with seen_lock:
                seen.append(lk)

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(worker, range(n)))

        self.assertEqual(len(seen), n)
        first = seen[0]
        for lk in seen:
            self.assertIs(lk, first, '_file_lock() devolvió locks distintos')


# ---------------------------------------------------------------------------
# 4) _OLLAMA_SEMAPHORES
# ---------------------------------------------------------------------------
class TestOllamaSemaphore(unittest.TestCase):
    """_ollama_sem(name) debe respetar el límite (CPU=1, GPU=2 por defecto)."""

    def setUp(self):
        with harness._OLLAMA_SEM_GUARD:
            harness._OLLAMA_SEMAPHORES.clear()

    def tearDown(self):
        # Semáforos no requieren release explícito entre tests.
        with harness._OLLAMA_SEM_GUARD:
            harness._OLLAMA_SEMAPHORES.clear()

    def test_ollama_semaphore_limit(self):
        """10 threads al CPU model → nunca más de 1 dentro a la vez."""
        sem = harness._ollama_sem('qwen3-coder:80b')
        in_flight = 0
        max_in_flight = 0
        cc_lock = threading.Lock()
        n = 10
        barrier = threading.Barrier(n)
        hold_sec = 0.1

        def worker(_i):
            nonlocal in_flight, max_in_flight
            barrier.wait()
            self.assertTrue(sem.acquire(blocking=True, timeout=5),
                            'no se pudo adquirir el semáforo CPU')
            try:
                with cc_lock:
                    in_flight += 1
                    max_in_flight = max(max_in_flight, in_flight)
                time.sleep(hold_sec)
            finally:
                with cc_lock:
                    in_flight -= 1
                sem.release()

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(worker, range(n)))

        self.assertLessEqual(max_in_flight, 1,
                             f'CPU semaphore dejó pasar {max_in_flight} simultáneos (debe ser 1)')
        self.assertEqual(max_in_flight, 1,
                         'CPU semaphore nunca se usó concurrentemente ¿se testeó?')

    def test_ollama_semaphore_gpu(self):
        """10 threads al GPU model → nunca más de 2 dentro a la vez."""
        sem = harness._ollama_sem('qwen3-coder:30b')
        in_flight = 0
        max_in_flight = 0
        cc_lock = threading.Lock()
        n = 10
        barrier = threading.Barrier(n)
        hold_sec = 0.1

        def worker(_i):
            nonlocal in_flight, max_in_flight
            barrier.wait()
            self.assertTrue(sem.acquire(blocking=True, timeout=5),
                            'no se pudo adquirir el semáforo GPU')
            try:
                with cc_lock:
                    in_flight += 1
                    max_in_flight = max(max_in_flight, in_flight)
                time.sleep(hold_sec)
            finally:
                with cc_lock:
                    in_flight -= 1
                sem.release()

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(worker, range(n)))

        self.assertLessEqual(max_in_flight, 2,
                             f'GPU semaphore dejó pasar {max_in_flight} (límite 2)')
        self.assertGreaterEqual(max_in_flight, 2,
                                'GPU semaphore nunca alcanzó el límite ¿se testeó?')

    def test_ollama_semaphore_lookup_consistent(self):
        """_ollama_sem(name) llamado desde N threads devuelve el mismo objeto."""
        n = 50
        barrier = threading.Barrier(n)
        seen = []
        seen_lock = threading.Lock()

        def worker(_i):
            barrier.wait()
            s = harness._ollama_sem('qwen3-coder:30b')
            with seen_lock:
                seen.append(s)

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(worker, range(n)))

        self.assertEqual(len(seen), n)
        first = seen[0]
        for s in seen:
            self.assertIs(s, first)


# ---------------------------------------------------------------------------
# 5) Health / quarantine
# ---------------------------------------------------------------------------
class TestHealthConcurrency(unittest.TestCase):
    """_mark_health debe ser consistente bajo concurrencia."""

    def setUp(self):
        with harness._HEALTH_LOCK:
            harness._HEALTH.clear()

    def tearDown(self):
        with harness._HEALTH_LOCK:
            harness._HEALTH.clear()

    def test_health_mark_concurrent(self):
        """100 threads, 10 modelos, todos ok=True → cada modelo tiene 10 oks."""
        n = 100
        n_models = 10
        barrier = threading.Barrier(n)
        errors = []
        models = [f'claude:claude-haiku-4-{i}' for i in range(n_models)]

        def worker(i):
            try:
                barrier.wait()
                mid = models[i % n_models]
                harness._mark_health(mid, ok=True)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(worker, range(n)))

        self.assertEqual(errors, [], f'crashes: {errors}')
        with harness._HEALTH_LOCK:
            self.assertEqual(len(harness._HEALTH), n_models)
            for mid in models:
                entry = harness._HEALTH[mid]
                self.assertEqual(entry['calls'], n // n_models,
                                 f'{mid}: calls={entry["calls"]}')
                self.assertEqual(entry['oks'], n // n_models)
                self.assertEqual(entry['fails'], 0)

    def test_health_quarantine_concurrent(self):
        """100 threads fallando el mismo modelo → quarantined_until activo, fails=100."""
        model_id = 'opencode:mimo-v2.5-free'
        n = 100
        barrier = threading.Barrier(n)
        errors = []

        def worker(_i):
            try:
                barrier.wait()
                harness._mark_health(model_id, ok=False)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(worker, range(n)))

        self.assertEqual(errors, [], f'crashes: {errors}')
        self.assertFalse(harness.is_healthy(model_id))
        self.assertIn(model_id, harness.quarantined_models())
        with harness._HEALTH_LOCK:
            entry = harness._HEALTH[model_id]
            self.assertEqual(entry['fails'], n,
                             f'fails={entry["fails"]}, esperaba {n}')
            self.assertEqual(entry['calls'], n)
            self.assertGreater(entry['quarantined_until'], time.time())

    def test_health_mark_mixed_ok_fail(self):
        """100 threads con ok mixto no corrompe contadores (calls == n, oks == ok_count)."""
        model_id = 'gemini:gemini-2.5-pro'
        n = 100
        barrier = threading.Barrier(n)
        results = {'oks': 0, 'fails': 0}
        res_lock = threading.Lock()

        def worker(i):
            barrier.wait()
            ok = (i % 2 == 0)
            harness._mark_health(model_id, ok=ok)
            with res_lock:
                if ok:
                    results['oks'] += 1
                else:
                    results['fails'] += 1

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(worker, range(n)))

        with harness._HEALTH_LOCK:
            entry = harness._HEALTH[model_id]
            # Calls SIEMPRE == n (cada call incrementa, protegido por lock).
            self.assertEqual(entry['calls'], n,
                             f"calls={entry['calls']}, esperaba {n}")
            # oks es monotónico: cuenta de ok=True. Si se intercalan fails
            # después, oks no decrementa. Por tanto oks == ok_count.
            self.assertEqual(entry['oks'], results['oks'],
                             f"oks={entry['oks']} != {results['oks']}")
            # fails es el contador CONSECUTIVO (se resetea con cada ok=True).
            # Por tanto <= total de fails observados.
            self.assertLessEqual(entry['fails'], results['fails'],
                                 f"fails={entry['fails']} > {results['fails']}")


# ---------------------------------------------------------------------------
# 6) Quota
# ---------------------------------------------------------------------------
class TestQuotaConcurrency(unittest.TestCase):
    """quota.record, local_spend, refresh bajo concurrencia."""

    def setUp(self):
        _reset_quota_state()

    def tearDown(self):
        _reset_quota_state()

    def test_quota_record_no_loss(self):
        """100 threads × N records → call_log tiene exactamente N×100 entries."""
        n_threads = 100
        per_thread = 5

        def worker(_i):
            for j in range(per_thread):
                quota.record(f'model-{j % 3}', 10, 5)

        with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as ex:
            list(ex.map(worker, range(n_threads)))

        self.assertEqual(len(quota._STATE.call_log), n_threads * per_thread)

    def test_quota_local_spend_concurrent(self):
        """100 threads intercalando record() y local_spend() no debe crashear."""
        n_threads = 100
        barrier = threading.Barrier(n_threads)
        errors = []

        def writer(_i):
            try:
                barrier.wait()
                for j in range(20):
                    quota.record(f'w-model-{j % 4}', 100, 50)
                    time.sleep(0.0001)
            except Exception as e:  # noqa: BLE001
                errors.append(('writer', e))

        def reader(_i):
            try:
                barrier.wait()
                for _ in range(20):
                    spend = quota.local_spend()
                    self.assertIsInstance(spend, dict)
                    time.sleep(0.0001)
            except Exception as e:  # noqa: BLE001
                errors.append(('reader', e))

        with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as ex:
            futs = []
            for i in range(n_threads // 2):
                futs.append(ex.submit(writer, i))
                futs.append(ex.submit(reader, i))
            for f in futs:
                f.result(timeout=10)

        self.assertEqual(errors, [], f'errores: {errors[:3]}')
        # El log tiene 50 writers × 20 records = 1000 entradas.
        self.assertEqual(len(quota._STATE.call_log), (n_threads // 2) * 20)
        spend = quota.local_spend()
        self.assertEqual(sum(s['calls'] for s in spend.values()),
                         (n_threads // 2) * 20)

    def test_quota_refresh_concurrent(self):
        """10 threads llaman refresh() → termina en ≤10s, no deadlock."""
        # Mock _run_ai_usage para no tocar ai-usage.sh (subprocess caro).
        n = 10
        barrier = threading.Barrier(n)
        errors = []
        results = []
        res_lock = threading.Lock()

        def worker(i):
            try:
                barrier.wait()
                # Force=True para que todos llamen a _run_ai_usage, exponiendo
                # contención bajo el lock de _STATE.
                quota.refresh(force=True)
                with res_lock:
                    results.append(i)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        with patch('quota._run_ai_usage', return_value={'daily': []}):
            with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
                futs = [ex.submit(worker, i) for i in range(n)]
                # 10s timeout: si hay deadlock, future.result lo levanta.
                for f in futs:
                    f.result(timeout=10)

        self.assertEqual(errors, [], f'errores: {errors}')
        self.assertEqual(len(results), n)

    def test_quota_by_model_pct_concurrent_writes(self):
        """by_model_pct se modifica bajo lock — lecturas concurrentes seguras."""
        n_threads = 50
        per_thread = 10
        barrier = threading.Barrier(n_threads)
        errors = []

        def writer(_i):
            try:
                barrier.wait()
                for j in range(per_thread):
                    with quota._STATE.lock:
                        quota._STATE.by_model_pct[f'model-{j}'] = {'daily': 0.0}
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def reader(_i):
            try:
                barrier.wait()
                for _ in range(per_thread):
                    quota.refresh()
                    _ = dict(quota._STATE.by_model_pct)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        with patch('quota._run_ai_usage', return_value={'daily': []}):
            with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as ex:
                futs = []
                for i in range(n_threads // 2):
                    futs.append(ex.submit(writer, i))
                    futs.append(ex.submit(reader, i))
                for f in futs:
                    f.result(timeout=10)

        self.assertEqual(errors, [], f'errores: {errors[:3]}')


# ---------------------------------------------------------------------------
# 7) MEM (memory.py) — record_bug y record_false_positive
# ---------------------------------------------------------------------------
class TestMemoryConcurrency(unittest.TestCase):
    """MEM.record_bug y MEM.record_false_positive bajo carga concurrente."""

    def setUp(self):
        # Crear una Memory limpia apuntando a un archivo temporal.
        import tempfile
        from pathlib import Path
        tmpdir = tempfile.mkdtemp(prefix='concurrency-mem-')
        self.path = Path(tmpdir) / 'memory.json'
        self.mem = memory.Memory()
        self._tmpdir = tmpdir

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_memory_concurrent_record_bug(self):
        """100 threads al mismo hash → exactamente 1 bug, times_confirmed=100."""
        n = 100
        barrier = threading.Barrier(n)
        errors = []

        def worker():
            try:
                barrier.wait()
                self.mem.record_bug('a.py', 1, 'foo bar baz')
                time.sleep(0.001)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f'errores: {errors}')
        rec = self.mem.get_bug('a.py', 1, 'foo bar baz')
        self.assertIsNotNone(rec)
        self.assertEqual(len(self.mem.bugs), 1,
                         f'se crearon {len(self.mem.bugs)} bugs; esperaba 1')
        self.assertEqual(rec.times_confirmed, n,
                         f'times_confirmed={rec.times_confirmed}; esperaba {n}')
        self.assertEqual(self.mem.total_bugs_found, 1)

    def test_memory_concurrent_record_bug_distinct(self):
        """100 threads con hashes distintos → 100 bugs distintos."""
        n = 100
        barrier = threading.Barrier(n)
        errors = []

        def worker(i):
            try:
                barrier.wait()
                self.mem.record_bug(f'f{i}.py', i, f'unique snippet {i}')
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f'errores: {errors}')
        self.assertEqual(len(self.mem.bugs), n)
        self.assertEqual(self.mem.total_bugs_found, n)

    def test_memory_concurrent_record_false_positive(self):
        """100 threads al mismo fp → 1 entrada, times_seen=100."""
        n = 100
        barrier = threading.Barrier(n)
        errors = []

        def worker():
            try:
                barrier.wait()
                self.mem.record_false_positive('a.py', 1, 'noise pattern')
                time.sleep(0.001)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f'errores: {errors}')
        h = memory.hash_suspect('a.py', 1, 'noise pattern')
        self.assertIn(h, self.mem.false_positives)
        self.assertEqual(self.mem.false_positives[h].times_seen, n)


# ---------------------------------------------------------------------------
# 8) Colas suspectq / bugq
# ---------------------------------------------------------------------------
class TestQueuesConcurrency(unittest.TestCase):
    """Producer-consumer sobre suspectq / bugq no debe perder items."""

    def setUp(self):
        # Las colas son singleton del módulo; las vaciamos si tienen algo.
        _drain_queue(harness.suspectq)
        _drain_queue(harness.bugq)

    def tearDown(self):
        _drain_queue(harness.suspectq)
        _drain_queue(harness.bugq)

    def test_suspectq_thread_safe(self):
        """10 producers + 10 consumers en suspectq → todos los items llegan."""
        n_producers = 10
        n_consumers = 10
        items_per_producer = 20
        expected = n_producers * items_per_producer

        # Crear Suspect dummy sin tocar filesystem (Suspect.post_init exige
        # archivo válido si se llama hash_suspect — pero ese solo lee args).
        def make_suspect(i):
            s = harness.Suspect(
                file=f'fake-{i}.py', line=1,
                snippet=f'snippet {i}', why='test',
            )
            return s

        def producer(i):
            for j in range(items_per_producer):
                harness.suspectq.put(make_suspect(i * items_per_producer + j))
            return expected

        consumed = []
        con_lock = threading.Lock()
        stop_consumers = threading.Event()
        consumers_started = threading.Barrier(n_consumers)
        consumers_consumed = []

        def consumer(_i):
            consumers_started.wait()
            local = []
            try:
                while True:
                    try:
                        s = harness.suspectq.get(timeout=0.5)
                        local.append(s)
                        harness.suspectq.task_done()
                    except queue.Empty:
                        if stop_consumers.is_set() and harness.suspectq.empty():
                            break
                        continue
            finally:
                with con_lock:
                    consumers_consumed.extend(local)

        consumers = [threading.Thread(target=consumer, args=(i,))
                     for i in range(n_consumers)]
        for c in consumers:
            c.start()

        with concurrent.futures.ThreadPoolExecutor(max_workers=n_producers) as ex:
            list(ex.map(producer, range(n_producers)))

        # Esperar a que la cola se vacíe.
        deadline = time.time() + 5
        while not harness.suspectq.empty() and time.time() < deadline:
            time.sleep(0.05)
        stop_consumers.set()
        for c in consumers:
            c.join(timeout=5)

        total = len(consumers_consumed)
        self.assertEqual(total, expected,
                         f'suspectq perdió {expected - total} items')

    def test_bugq_thread_safe(self):
        """10 producers + 10 consumers en bugq → todos los items llegan."""
        n_producers = 10
        n_consumers = 10
        items_per_producer = 10
        expected = n_producers * items_per_producer

        def make_bug(i):
            sus = harness.Suspect(
                file=f'fake-{i}.py', line=1,
                snippet=f'snippet {i}', why='test',
            )
            return harness.Bug(suspect=sus)

        def producer(i):
            for j in range(items_per_producer):
                harness.bugq.put(make_bug(i * items_per_producer + j))

        consumed = []
        con_lock = threading.Lock()
        stop_consumers = threading.Event()
        consumers_started = threading.Barrier(n_consumers)

        def consumer(_i):
            consumers_started.wait()
            local = []
            try:
                while True:
                    try:
                        b = harness.bugq.get(timeout=0.5)
                        local.append(b)
                        harness.bugq.task_done()
                    except queue.Empty:
                        if stop_consumers.is_set() and harness.bugq.empty():
                            break
                        continue
            finally:
                with con_lock:
                    consumed.extend(local)

        consumers = [threading.Thread(target=consumer, args=(i,))
                     for i in range(n_consumers)]
        for c in consumers:
            c.start()

        with concurrent.futures.ThreadPoolExecutor(max_workers=n_producers) as ex:
            list(ex.map(producer, range(n_producers)))

        deadline = time.time() + 5
        while not harness.bugq.empty() and time.time() < deadline:
            time.sleep(0.05)
        stop_consumers.set()
        for c in consumers:
            c.join(timeout=5)

        total = len(consumed)
        self.assertEqual(total, expected,
                         f'bugq perdió {expected - total} items')


# ---------------------------------------------------------------------------
# 9) Workload mixto (hunters + filtros + jueces)
# ---------------------------------------------------------------------------
class TestMixedWorkload(unittest.TestCase):
    """Simula un ciclo hunters → suspectq → filtros → bugq → jueces.

    El objetivo es detectar deadlocks cuando múltiples roles del harness
    compiten por STATS_LOCK, THREAD_STATES_LOCK, _HEALTH_LOCK y colas.
    """

    def setUp(self):
        _reset_harness_state()
        _reset_quota_state()
        _drain_queue(harness.suspectq)
        _drain_queue(harness.bugq)

    def tearDown(self):
        _reset_harness_state()
        _reset_quota_state()
        _drain_queue(harness.suspectq)
        _drain_queue(harness.bugq)

    def test_mixed_workload(self):
        """Workload completo: producers, filters y judges corren 5s sin deadlock."""
        duration_sec = 2.0
        stop = threading.Event()
        errors = []
        err_lock = threading.Lock()

        def hunter(wid: int) -> None:
            try:
                while not stop.is_set():
                    with harness.STATS_LOCK:
                        harness.STATS['rounds'] += 1
                    s = harness.Suspect(
                        file=f'app/(panel)/z{wid}/page.tsx',
                        line=1, snippet=f'code {wid}', why='hunt',
                        source='hunter',
                    )
                    # Ojo: yield_suspect chequea WT_SUBDIR y descarta fantasmas.
                    # Llamamos directo a la cola para no depender del FS.
                    try:
                        harness.suspectq.put_nowait(s)
                    except queue.Full:
                        pass
                    harness.update_thread_state(
                        f'hunter-{wid}', kind='hunter',
                        model='test', status='busy', calls=1,
                    )
                    _mark_local(f'claude:haiku-{wid}', ok=True)
                    quota.record(f'claude:haiku-{wid}', 100, 50)
                    time.sleep(0.001)
            except Exception as e:  # noqa: BLE001
                with err_lock:
                    errors.append(('hunter', e))

        def filter_worker(wid: int) -> None:
            try:
                while not stop.is_set():
                    try:
                        s = harness.suspectq.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    harness.update_thread_state(
                        f'filter-{wid}', kind='filter',
                        model='test', status='busy', calls=1,
                    )
                    time.sleep(0.001)
                    # 50/50 REAL vs NOT_A_BUG para tener flujo en ambas direcciones.
                    if wid % 2 == 0:
                        try:
                            harness.bugq.put_nowait(harness.Bug(suspect=s))
                        except queue.Full:
                            pass
                    else:
                        with harness.STATS_LOCK:
                            harness.STATS['suspects_refuted_by_filter'] += 1
                    harness.suspectq.task_done()
            except Exception as e:  # noqa: BLE001
                with err_lock:
                    errors.append(('filter', e))

        def judge(wid: int) -> None:
            try:
                while not stop.is_set():
                    try:
                        b = harness.bugq.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    harness.update_thread_state(
                        f'judge-{wid}', kind='judge',
                        model='test', status='busy', calls=1,
                    )
                    time.sleep(0.001)
                    # Confirma siempre para acumular STATS.
                    with harness.STATS_LOCK:
                        harness.STATS['bugs_confirmed'] += 1
                    harness.bugq.task_done()
            except Exception as e:  # noqa: BLE001
                with err_lock:
                    errors.append(('judge', e))

        threads = []
        for i in range(4):
            threads.append(threading.Thread(target=hunter, args=(i,), daemon=True))
            threads.append(threading.Thread(target=filter_worker, args=(i,), daemon=True))
            threads.append(threading.Thread(target=judge, args=(i,), daemon=True))

        t0 = time.time()
        for t in threads:
            t.start()

        # Timeout duro: si hay deadlock, no llegamos aquí.
        time.sleep(duration_sec)
        stop.set()

        # Drain y join con timeout.
        for t in threads:
            t.join(timeout=2.0)

        elapsed = time.time() - t0
        self.assertLess(elapsed, duration_sec + 4.0,
                        f'tardó {elapsed:.1f}s; probable deadlock')
        # Filtrar hilos daemon zombi (timeout) — los vivos no nos preocupan
        # para esta aserción (el test de no-deadlock pasa si terminan).
        alive = [t for t in threads if t.is_alive()]
        self.assertEqual(len(alive), 0,
                         f'{len(alive)} hilos colgados (posible deadlock)')
        self.assertEqual(errors, [], f'errores en workers: {errors[:3]}')

        # Verificar que hubo progreso real.
        with harness.STATS_LOCK:
            rounds = harness.STATS['rounds']
            confirmed = harness.STATS['bugs_confirmed']
        self.assertGreater(rounds, 0, 'ningún hunter incrementó rounds')
        self.assertGreater(len(quota._STATE.call_log), 0,
                           'ninguna llamada a quota.record')


def _mark_local(model_id: str, ok: bool) -> None:
    """Helper: marca salud sin loggear (silencia la salida en tests)."""
    # Replicar la lógica de _mark_health sin el `log()` para no ensuciar stderr.
    with harness._HEALTH_LOCK:
        h = harness._HEALTH.setdefault(
            model_id,
            {'fails': 0, 'quarantined_until': 0.0, 'calls': 0, 'oks': 0},
        )
        h['calls'] += 1
        if ok:
            h['oks'] += 1
            h['fails'] = 0
            if h['quarantined_until'] <= time.time():
                h['quarantined_until'] = 0.0
        else:
            h['fails'] += 1


if __name__ == '__main__':
    unittest.main(verbosity=2)