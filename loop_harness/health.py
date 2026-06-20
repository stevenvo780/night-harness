"""Model health and quarantine helpers."""
from __future__ import annotations

import concurrent.futures
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any


HealthMap = dict[str, dict[str, Any]]
LogFn = Callable[[str], None]


def mark_health(
    health: HealthMap,
    lock: threading.Lock,
    *,
    model_id: str,
    ok: bool,
    fail_threshold: int,
    quarantine_secs: int,
    log: LogFn,
) -> None:
    """Record one model call result and quarantine after repeated failures."""
    with lock:
        h = health.setdefault(model_id, {
            'fails': 0,
            'quarantined_until': 0.0,
            'calls': 0,
            'oks': 0,
        })
        h['calls'] += 1
        if ok:
            h['oks'] += 1
            h['fails'] = 0
            if h['quarantined_until'] <= time.time():
                h['quarantined_until'] = 0.0
            return

        h['fails'] += 1
        if h['fails'] >= fail_threshold and h['quarantined_until'] <= time.time():
            h['quarantined_until'] = time.time() + quarantine_secs
            log(f'  [health] {model_id} -> CUARENTENA {quarantine_secs}s '
                f'({h["fails"]} fallos seguidos)')


def is_healthy(health: HealthMap, lock: threading.Lock, model_id: str) -> bool:
    """Return whether a model can currently be used."""
    with lock:
        h = health.get(model_id)
        if not h:
            return True
        now = time.time()
        quarantine_until = h.get('quarantined_until', 0.0)
        if now >= quarantine_until and quarantine_until > 0:
            h['fails'] = 0
            h['quarantined_until'] = 0.0
        return now >= quarantine_until


def quarantined_models(health: HealthMap, lock: threading.Lock) -> list[str]:
    """Return model ids whose quarantine window is still active."""
    now = time.time()
    with lock:
        return sorted(
            model for model, data in health.items()
            if now < data.get('quarantined_until', 0.0)
        )


def seed_quarantine(
    health: HealthMap,
    lock: threading.Lock,
    ids: Iterable[str],
    *,
    fail_threshold: int,
    quarantine_secs: int,
    log: LogFn,
) -> None:
    """Pre-quarantine known-zombie models at startup."""
    ids = tuple(ids)
    if not ids:
        return
    with lock:
        for model_id in ids:
            h = health.setdefault(model_id, {
                'fails': 0,
                'quarantined_until': 0.0,
                'calls': 0,
                'oks': 0,
            })
            h['fails'] = fail_threshold
            h['quarantined_until'] = time.time() + quarantine_secs
    log(f'  [health] cuarentena inicial sembrada: {list(ids)}')


def health_probe_all(
    specs,
    *,
    call_model: Callable[..., str | None],
    health: HealthMap,
    lock: threading.Lock,
    fail_threshold: int,
    quarantine_secs: int,
    log: LogFn,
    timeout: int = 25,
    workers: int = 8,
) -> None:
    """Probe unique model specs and quarantine the ones that do not answer."""
    seen = {}
    for spec in specs:
        seen.setdefault(spec.id, spec)

    def _probe(spec) -> None:
        try:
            text = call_model(spec, '', 'Responde solo: OK', max_tokens=8, timeout=timeout)
        except Exception:
            text = None
        if text and text.strip():
            log(f'  [health] probe OK {spec.id}')
            return
        with lock:
            h = health.setdefault(spec.id, {
                'fails': 0,
                'quarantined_until': 0.0,
                'calls': 0,
                'oks': 0,
            })
            h['fails'] = max(h['fails'], fail_threshold)
            h['quarantined_until'] = time.time() + quarantine_secs
        log(f'  [health] probe FALLO {spec.id} -> cuarentena')

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(_probe, seen.values()))

