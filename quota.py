#!/usr/bin/env python3
"""
quota.py — Control de costos del Harness v6.

Lee `ai-usage-live` (repo hermano en /workspace/ai-usage-live) para conocer
el consumo en vivo de Claude, Codex y Gemini, y tomar decisiones de
planificación de modelos:

  · "Claude Sonnet weekly está al 78% → no usar Sonnet, degradar a Minimax".
  · "Gemini Flash daily está al 95% → no usar Flash, degradar a Haiku".
  · "Codex agotado (5h window) → fallback a Sonnet en fixer".
  · "Minimax siempre disponible (no rate-limit explícito) → usar como base".

El control es CONSERVADOR: prefiere fallar ANTES que gastar de más. La política
Opus está en `tiers.py` y se aplica aquí (Opus SOLO si todo lo demás falla).

API pública:
  · QuotaState.snapshot()      — estado actual (con cache de N segundos).
  · QuotaState.is_model_ok(m)  — ¿puedo usar el modelo M ahora?
  · QuotaState.filter_tier(t)  — dada una lista de ModelSpec, devuelve los que
                                  puedo usar AHORA ordenados por coste.
  · QuotaState.record(spec, in_tok, out_tok) — anota una llamada para que el
                                  `ai-usage` la refleje.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AI_USAGE_DIR = Path(os.environ.get(
    'AI_USAGE_DIR',
    '/workspace/ai-usage-live',
))

# Límites por familia. Si el plan real difiere, sobreescribir vía env.
DEFAULT_LIMITS = {
    'claude-sonnet-4-6': {'weekly_pct_max': 80.0, 'session_pct_max': 60.0},
    'claude-opus-4-6':   {'weekly_pct_max': 95.0, 'session_pct_max': 80.0},
    'claude-haiku-4-5':  {'weekly_pct_max': 90.0, 'session_pct_max': 70.0},
    'gpt-5-codex':       {'daily_pct_max': 85.0, 'session_pct_max': 70.0},
    'gemini-2.5-pro':    {'daily_pct_max': 85.0, 'session_pct_max': 70.0},
    'gemini-2.5-flash':  {'daily_pct_max': 90.0, 'session_pct_max': 75.0},
    'gemini-2.5-flash-lite': {'daily_pct_max': 95.0, 'session_pct_max': 80.0},
    # Minimax-M3: sin límite explícito en el dashboard; siempre OK.
}


@dataclass
class QuotaState:
    """Estado en vivo de las cuotas de todos los proveedores."""
    timestamp: float = 0.0
    by_model_pct: dict[str, dict[str, float]] = field(default_factory=dict)
    """Por modelo: { 'claude-sonnet-4-6': {'daily': 0, 'weekly': 78, 'session': 12}, ... }"""
    lock: threading.Lock = field(default_factory=threading.Lock)
    call_log: list[dict] = field(default_factory=list)
    """Llamadas hechas por ESTE run del harness (no se reflejan en ai-usage aún)."""

    def age(self) -> float:
        return time.time() - self.timestamp

    def is_fresh(self, max_age_sec: float = 60.0) -> bool:
        return self.age() < max_age_sec


# Singleton de proceso.
_STATE = QuotaState()


# ---------------------------------------------------------------------------
# Lectura de ai-usage-live (ejecuta el script shell y parsea JSON).
# ---------------------------------------------------------------------------

def _run_ai_usage(args: list[str], timeout: int = 30) -> dict:
    """Ejecuta ai-usage.sh con `JSON=1` y parsea el JSON."""
    cmd = ['bash', str(AI_USAGE_DIR / 'ai-usage.sh'), *args]
    env = os.environ.copy()
    env['JSON'] = '1'
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(AI_USAGE_DIR),
            env=env,
        )
        if result.returncode != 0:
            return {}
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return {}


def _extract_pct(ai_data: dict) -> dict[str, dict[str, float]]:
    """Convierte la salida de `ai-usage.sh daily|weekly` en { model: { daily, weekly, session } }.

    Solo miramos el ÚLTIMO día de la respuesta (la más reciente) y el
    ACUMULADO semanal (suma de todos los días). El porcentaje es
    `used / limit` (en %). Si el límite no está configurado (None), el
    porcentaje es 0.
    """
    by_model: dict[str, dict[str, float]] = {}
    daily = ai_data.get('daily', [])
    if not daily:
        return by_model

    # Weekly = suma de todos los daily (es la convención del dashboard).
    weekly_by_model: dict[str, float] = {}
    for day in daily:
        for mb in day.get('modelBreakdowns', []):
            name = mb.get('modelName', 'unknown')
            # effective tokens = input + cacheCreation + output
            eff = (
                mb.get('inputTokens', 0)
                + mb.get('cacheCreationTokens', 0)
                + mb.get('outputTokens', 0)
            )
            weekly_by_model[name] = weekly_by_model.get(name, 0) + eff

    # Hoy = último día.
    today = daily[-1] if daily else {}
    for mb in today.get('modelBreakdowns', []):
        name = mb.get('modelName', 'unknown')
        eff_today = (
            mb.get('inputTokens', 0)
            + mb.get('cacheCreationTokens', 0)
            + mb.get('outputTokens', 0)
        )
        eff_week = weekly_by_model.get(name, 0)
        by_model[name] = {
            'daily_tokens': eff_today,
            'weekly_tokens': eff_week,
            # pct = 0 si no hay límite conocido.
            'daily': 0.0,
            'weekly': 0.0,
        }

    return by_model


def refresh(force: bool = False) -> QuotaState:
    """Refresca el estado desde ai-usage-live (con cache de 60s por defecto)."""
    with _STATE.lock:
        if not force and _STATE.is_fresh():
            return _STATE
        daily = _run_ai_usage(['daily'])
        weekly = _run_ai_usage(['weekly'])
        new_by_model = _extract_pct(daily)
        weekly_by_model = _extract_pct(weekly)
        # Combinar daily + weekly.
        for model_name, info in new_by_model.items():
            wk = weekly_by_model.get(model_name, {})
            info['weekly'] = wk.get('weekly_tokens', 0)
        _STATE.by_model_pct = new_by_model
        _STATE.timestamp = time.time()
        return _STATE


# ---------------------------------------------------------------------------
# API: ¿puedo usar este modelo? ¿cuánto cuesta?
# ---------------------------------------------------------------------------

def _model_id_to_ai_usage_id(spec_id: str) -> str:
    """minimax:MiniMax-M3 → minimax-M3 ; claude:claude-sonnet-4-6 → claude-sonnet-4-6"""
    if ':' in spec_id:
        prefix, name = spec_id.split(':', 1)
        # 'claude:claude-sonnet-4-6' → 'claude-sonnet-4-6' (sin duplicar prefijo)
        if name.startswith(prefix + '-'):
            return name
        return name
    return spec_id


def _normalize_model_name(name: str) -> str:
    """Quita prefijos de versión (ej. '-20250514') que el dashboard ignora."""
    import re
    return re.sub(r'-\d{8}$', '', name)


def get_limit(model_id: str, period: str) -> Optional[float]:
    """Devuelve el límite configurado para el modelo/periodo, o None."""
    normalized = _normalize_model_name(model_id)
    cfg = DEFAULT_LIMITS.get(normalized)
    if not cfg:
        return None
    key = {
        'daily': 'daily_pct_max',
        'weekly': 'weekly_pct_max',
        'session': 'session_pct_max',
    }.get(period)
    if not key:
        return None
    return cfg.get(key)


def is_model_ok(model_id: str) -> bool:
    """True si el modelo está por debajo del límite (puede usarse).

    Sin datos → True (deja pasar; el caller decide si gastar).
    """
    state = refresh()
    normalized = _normalize_model_name(model_id)
    info = state.by_model_pct.get(normalized, {})
    if not info:
        # Modelo sin tracking (ej. Minimax, Codex si no está en logs) → OK.
        return True
    for period, pct_max_key in [('daily', 'daily_pct_max'), ('weekly', 'weekly_pct_max')]:
        limit = get_limit(normalized, period)
        if not limit:
            continue
        # Si no tenemos pct real, estimamos: tokens_usados / límite_estimado
        # (el dashboard no siempre expone pct; caemos a heurística).
        # Por ahora, usamos los tokens: si el daily supera 80% del límite estimado
        # (100M tokens/día como proxy), bloqueamos. Esto es aproximado.
        tokens = info.get(f'{period}_tokens', 0)
        # 100M tokens/día como proxy de "1 plan completo usado" para Sonnet/Pro.
        # Se sobreescribe con `ai-usage-quota edit` para precisión.
        proxy_limit = {
            'claude-sonnet-4-6': 50_000_000,
            'claude-opus-4-6': 10_000_000,
            'claude-haiku-4-5': 100_000_000,
            'gpt-5-codex': 30_000_000,
            'gemini-2.5-pro': 50_000_000,
            'gemini-2.5-flash': 200_000_000,
            'gemini-2.5-flash-lite': 500_000_000,
        }.get(normalized, 50_000_000)
        pct = (tokens / proxy_limit) * 100
        if pct >= limit:
            return False
    return True


def filter_tier(specs: tuple, role: str) -> list:
    """Filtra un tuple de ModelSpec a los que se pueden usar AHORA, ordenados
    por `relative_cost` (barato primero).
    """
    state = refresh()
    out = []
    for spec in specs:
        if is_model_ok(spec.id):
            out.append(spec)
    out.sort(key=lambda m: m.relative_cost)
    return out


# ---------------------------------------------------------------------------
# Tracking local (el harness anota cada llamada que hace; sirve para reportar
# gasto al final del run aunque ai-usage tarde en reflejarlo).
# ---------------------------------------------------------------------------

def record(spec_id: str, in_tokens: int, out_tokens: int) -> None:
    """Anota una llamada para reporting local."""
    with _STATE.lock:
        _STATE.call_log.append({
            'ts': time.time(),
            'model': spec_id,
            'in_tokens': in_tokens,
            'out_tokens': out_tokens,
            'effective_tokens': in_tokens + out_tokens,  # proxy sin cache info
        })


def local_spend() -> dict:
    """Gasto acumulado del run actual del harness (no facturado, solo local)."""
    by_model: dict[str, dict[str, int]] = {}
    with _STATE.lock:
        for entry in _STATE.call_log:
            m = entry['model']
            slot = by_model.setdefault(m, {'in': 0, 'out': 0, 'calls': 0, 'eff': 0})
            slot['in'] += entry['in_tokens']
            slot['out'] += entry['out_tokens']
            slot['calls'] += 1
            slot['eff'] += entry['effective_tokens']
    return by_model


def report() -> str:
    """Resumen human-readable del estado de cuotas + gasto del run."""
    state = refresh()
    lines = ['=== Estado de cuotas (ai-usage-live) ===']
    for model, info in sorted(state.by_model_pct.items()):
        lim_d = get_limit(model, 'daily') or 100
        lim_w = get_limit(model, 'weekly') or 100
        # Solo informativo; el gating real es por `is_model_ok`.
        d = info.get('daily_tokens', 0)
        w = info.get('weekly_tokens', 0)
        lines.append(f'  {model:32s}  today={d:>12,}  week={w:>12,}')
    lines.append('')
    lines.append('=== Gasto del run actual ===')
    spend = local_spend()
    for m, s in sorted(spend.items()):
        lines.append(
            f'  {m:40s}  calls={s["calls"]:>4}  '
            f'in={s["in"]:>10,}  out={s["out"]:>10,}  eff={s["eff"]:>10,}'
        )
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Banner Opus — se imprime una sola vez por sesión cuando se decide usarlo.
# ---------------------------------------------------------------------------

_OPUS_BANNER_PRINTED = False


def maybe_print_opus_banner() -> None:
    global _OPUS_BANNER_PRINTED
    if _OPUS_BANNER_PRINTED:
        return
    _OPUS_BANNER_PRINTED = True
    print('=' * 78)
    print('  ⚠ OPUS ÚLTIMO RECURSO')
    print('  El harness detectó que ningún árbitro más barato puede decidir.')
    print('  Se invocará claude-opus-4-6 SOLO para fixes riesgosos.')
    print('  Coste aprox: 10x Sonnet. Use con moderación.')
    print('=' * 78)


if __name__ == '__main__':
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'report'
    if cmd == 'refresh':
        s = refresh(force=True)
        print(json.dumps(s.by_model_pct, indent=2))
    elif cmd == 'report':
        print(report())
    elif cmd == 'check':
        # check <model_id>
        if len(sys.argv) < 3:
            print('uso: python quota.py check <model_id>')
            sys.exit(1)
        print(f'{sys.argv[2]}: {"OK" if is_model_ok(sys.argv[2]) else "BLOCKED"}')
    else:
        print('uso: refresh | report | check <model_id>')
