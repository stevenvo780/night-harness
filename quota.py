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
    'claude-sonnet-4-5': {'weekly_pct_max': 80.0, 'session_pct_max': 60.0},
    'claude-opus-4-6':   {'weekly_pct_max': 95.0, 'session_pct_max': 80.0},
    'claude-haiku-4-5':  {'weekly_pct_max': 90.0, 'session_pct_max': 70.0},
    'codex-mini':        {'daily_pct_max': 85.0, 'session_pct_max': 70.0},
    'gemini-2.5-pro':    {'daily_pct_max': 85.0, 'session_pct_max': 70.0},
    'gemini-2.5-flash':  {'daily_pct_max': 90.0, 'session_pct_max': 75.0},
    'gemini-2.5-flash-lite': {'daily_pct_max': 95.0, 'session_pct_max': 80.0},
    'gemini-3-pro-preview':   {'daily_pct_max': 85.0, 'session_pct_max': 70.0},
    'gemini-3-flash-preview': {'daily_pct_max': 95.0, 'session_pct_max': 80.0},
    # Minimax-M3: sin límite explícito en el dashboard; siempre OK.
}

# Fallback: tokens absolutos cuando `ai-usage.sh` no devuelve `pct_used`.
# El porcentaje se calcula como (tokens / PROXY_LIMITS[model]) * 100.
# Si el plan real difiere, ajustar aquí o sobrescribir `is_model_ok`/`_remaining_pct`.
PROXY_LIMITS: dict[str, int] = {
    'claude-sonnet-4-5': 50_000_000,
    'claude-sonnet-4-6': 50_000_000,
    'claude-opus-4-6': 10_000_000,
    'claude-haiku-4-5': 100_000_000,
    'codex-mini': 30_000_000,
    'gemini-2.5-pro': 50_000_000,
    'gemini-2.5-flash': 200_000_000,
    'gemini-2.5-flash-lite': 500_000_000,
    'gemini-3-pro-preview': 50_000_000,
    'gemini-3-flash-preview': 200_000_000,
}
DEFAULT_PROXY_LIMIT: int = 50_000_000


@dataclass
class QuotaState:
    """Estado en vivo de las cuotas de todos los proveedores."""
    timestamp: float = 0.0
    last_refresh_started: float = 0.0
    """Sentinel: timestamp del INICIO del último refresh. Usado por `refresh()`
    para evitar que dos hilos ejecuten el subprocess en paralelo dentro de la
    ventana de 60s.
    """
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
        info: dict[str, float] = {
            'daily_tokens': eff_today,
            'weekly_tokens': eff_week,
            # pct = 0 si no hay límite conocido.
            'daily': 0.0,
            'weekly': 0.0,
        }
        # Si el dashboard expone `pct_used` por modelo, lo guardamos: es la
        # fuente de verdad (no requiere conocer el límite absoluto del plan).
        if 'pct_used' in mb:
            try:
                info['pct_used'] = float(mb['pct_used'])
            except (TypeError, ValueError):
                pass
        by_model[name] = info

    return by_model


def refresh(force: bool = False) -> QuotaState:
    """Refresca el estado desde ai-usage-live (con cache de 60s por defecto).

    Thread-safe: usa `last_refresh_started` como sentinel. El primer hilo
    toma el lock, marca el timestamp de INICIO y suelta el lock; los hilos
    que lleguen dentro de la ventana de 60s ven que ya hay un refresh en
    curso (o recién terminado) y retornan sin volver a ejecutar el
    subprocess. El subprocess corre FUERA del lock para no bloquear a
    lectores concurrentes.
    """
    with _STATE.lock:
        now = time.time()
        if not force and (now - _STATE.last_refresh_started) < 60:
            return _STATE
        _STATE.last_refresh_started = now

    # Subprocess fuera del lock: no bloquea a otros lectores.
    daily = _run_ai_usage(['daily'])
    weekly = _run_ai_usage(['weekly'])
    new_by_model = _extract_pct(daily)
    weekly_by_model = _extract_pct(weekly)
    # Combinar daily + weekly.
    for model_name, info in new_by_model.items():
        wk = weekly_by_model.get(model_name, {})
        info['weekly'] = wk.get('weekly_tokens', 0)
        if 'pct_used' in wk and 'pct_used' not in info:
            info['pct_used'] = wk['pct_used']

    with _STATE.lock:
        # No pisar datos válidos con {} si `ai-usage.sh` falló (devolvió vacío):
        # eso desactivaría el control de cuota en silencio. Solo reemplazar si
        # hay datos nuevos, o si nunca tuvimos datos.
        if new_by_model or not _STATE.by_model_pct:
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
    """Quita prefijos de versión (ej. '-20250514') que el dashboard ignora.

    NO toca el `:` (un id Ollama como `qwen3-coder:30b` usa `:` para el tag de
    tamaño, no para un prefijo de proveedor). El pelado del prefijo de proveedor
    (`claude:`, `gemini:`, ...) lo hace `_model_id_to_ai_usage_id` en los call
    sites de cuota, donde los modelos locales ya fueron corto-circuitados.
    """
    import re
    return re.sub(r'-\d{8}$', '', name)


def get_limit(model_id: str, period: str) -> Optional[float]:
    """Devuelve el límite configurado para el modelo/periodo, o None."""
    normalized = _normalize_model_name(_model_id_to_ai_usage_id(model_id))
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

    REGLAS:
      · Modelos locales (`ollama:*`): SIEMPRE OK — son ILIMITADOS (no consumen
        quota cloud, la máquina es local y potente). Aprovechamos el paralelismo
        al máximo.
      · Modelos opencode / opencode-go (jul-2026, key del usuario): SIEMPRE OK.
        OpenCode no expone tracking de quota; el coste está implícito en la key.
        Si la key falla, el modelo retorna 401 y el caller cae al siguiente.
      · Sin datos del cloud → True (deja pasar; el caller decide si gastar).
      · Con datos del cloud → comparar contra DEFAULT_LIMITS.
    """
    # Local: infinito. No se consulta quota, no se registra en call_log.
    if model_id.startswith('ollama:'):
        return True

    # OpenCode (free + pro): sin tracking, asume OK. Si la key falla, el caller
    # lo descubre al invocar y cae al siguiente tier.
    if model_id.startswith('opencode:') or model_id.startswith('opencode-go:'):
        return True

    state = refresh()
    normalized = _normalize_model_name(_model_id_to_ai_usage_id(model_id))
    info = state.by_model_pct.get(normalized, {})
    if not info:
        # Modelo sin tracking (ej. Minimax, Codex si no está en logs) → OK.
        return True
    for period, pct_max_key in [('daily', 'daily_pct_max'), ('weekly', 'weekly_pct_max')]:
        limit = get_limit(normalized, period)
        if not limit:
            continue
        pct = _pct_used_for(info, period, normalized)
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


def _remaining_pct(model_id: str) -> float:
    """Devuelve el % de cuota RESTANTE para un modelo (100 = todo libre).

    Para modelos sin tracking (locales, opencode, sin datos) devuelve 100.
    Si está sobre el 100% de su límite, devuelve 0.
    """
    if model_id.startswith('ollama:') or model_id.startswith('opencode:'):
        return 100.0
    state = refresh()
    normalized = _normalize_model_name(_model_id_to_ai_usage_id(model_id))
    info = state.by_model_pct.get(normalized, {})
    if not info:
        return 100.0
    # El "más restrictivo" entre daily y weekly gana.
    worst = 100.0
    for period in ('daily', 'weekly'):
        limit = get_limit(normalized, period)
        if not limit:
            continue
        pct_used = _pct_used_for(info, period, normalized)
        pct_remaining = max(0.0, limit - pct_used)
        worst = min(worst, pct_remaining)
    return worst


def _pct_used_for(info: dict, period: str, normalized: str) -> float:
    """Devuelve el % de cuota usada por el modelo en el periodo dado.

    Prioridad:
      1. `pct_used` directo del dashboard (si está presente en la info).
      2. Fallback: (tokens / PROXY_LIMITS[model]) * 100.
    """
    direct = info.get('pct_used')
    if direct is not None:
        return float(direct)
    tokens = info.get(f'{period}_tokens', 0)
    proxy_limit = PROXY_LIMITS.get(normalized, DEFAULT_PROXY_LIMIT)
    if not proxy_limit:
        return 0.0
    return (tokens / proxy_limit) * 100


def smart_pick(specs: tuple, role: str) -> Optional['tiers.ModelSpec']:
    """Selector "abundancia": elige el MEJOR modelo disponible AHORA.

    Algoritmo:
      1. Filtra los specs que pasan `is_model_ok` (cuota OK).
      2. Para cada uno, calcula `remaining_pct` (cuota restante).
      3. Puntuación = remaining_pct * (10 - relative_cost) / 10
         (más cuota restante + más barato = mejor)
      4. Si todos están KO (cuota agotada), devuelve el de mayor `remaining_pct`
         (rotación de gracia: aunque esté casi agotado, lo usa brevemente para
         mantener la cola llena — el siguiente ciclo elegirá otro).

    NO es exhaustivo: un solo `best` por llamada. El caller itera si falla.
    """
    candidates = [s for s in specs if is_model_ok(s.id)]
    if not candidates:
        # Todos KO: devolver el de mayor remaining_pct (rotación).
        all_with_pct = [(s, _remaining_pct(s.id)) for s in specs]
        all_with_pct.sort(key=lambda kv: -kv[1])
        return all_with_pct[0][0] if all_with_pct else None
    scored = []
    for s in candidates:
        rem = _remaining_pct(s.id)
        # relative_cost 1-10: barato = 1, caro = 10. Invertimos: 10-cost (alto = bueno)
        # Calidad "implícita" = strength del spec + coste bajo.
        quality_hint = (11 - min(s.relative_cost, 10)) / 10.0
        # Score: combina remaining con calidad. Más remaining = usa más.
        # Más calidad = prefiere ese modelo (no el más barato).
        score = (rem / 100.0) * (0.6 + 0.4 * quality_hint)
        scored.append((s, score))
    scored.sort(key=lambda kv: -kv[1])
    return scored[0][0]


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
