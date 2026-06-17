#!/usr/bin/env python3
"""
tiers.py — Definición de tiers y cadenas de modelos para el Harness v6 "Adaptativo".

El v6 prioriza coste bajo y cadencia alta. Opus aparece SOLO como último recurso
en el árbitro, y solo si (a) hay un fix riesgoso (toca dinero, stock, estado,
auth), (b) los 3 árbitros anteriores discordaron o están en cuarentena, y
(c) hay cuota de Claude disponible. Se imprime un banner de aviso al usuario
cada vez que Opus se invoca.

Cada tier es una LISTA ordenada de `ModelSpec`. El harness los prueba en
orden y cae al siguiente si la cuota está agotada, el modelo está en
cuarentena, o falla tras N reintentos.

Las decisiones concretas (qué tier se usa para cada rol) las toma `quota.py`
según el estado en vivo de `ai-usage-live`. Este archivo es solo la
"topología": qué modelos existen, qué orden de preferencia base, y qué
capacidades tiene cada uno.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal, Optional

# ---------------------------------------------------------------------------
# Catálogo de modelos disponibles (lo que el harness PUEDE usar).
# Cada spec incluye: id canónico, "rol" (quién lo sirve), familia (para
# diversidad en consenso), y un coste relativo (1=barato, 10=caro) que el
# control de cuotas usa como heurística. NO es el precio oficial del API;
# es un peso para el planificador.
# ---------------------------------------------------------------------------

# Roles:
#   local       — Ollama / servidor local
#   cloud_mini  — modelos cloud baratos y rápidos (Minimax, Haiku, Flash Lite)
#   cloud_mid   — modelos cloud medios (Flash, Sonnet, Pro, Minimax-largo)
#   cloud_big   — modelos cloud grandes (Pro-thinking, Opus, Ultra)
#
# Familias (para diversidad en consenso):
#   qwen, minimax, gemini, claude, codex, local_qwen
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    """Especificación inmutable de un modelo que el harness puede invocar."""
    id: str
    """ID canónico que se pasa al CLI/SDK (ej. 'claude -p', 'gemini-2.5-flash', 'ollama:qwen3.5:9b')."""
    family: str
    """Familia para diversidad en consenso (qwen, minimax, gemini, claude, codex)."""
    role: Literal['local', 'cloud_mini', 'cloud_mid', 'cloud_big']
    relative_cost: int
    """1=barato, 10=caro. Usado por el planificador para evitar nubes caras cuando hay locales."""
    context_window: int = 200_000
    """Tokens de contexto. 0 = desconocido."""
    strengths: tuple = ()
    """Tags: 'code', 'vision', 'fast', 'reasoning', 'json', 'long', 'fix'."""
    command_template: Optional[str] = None
    """Si es None, se infiere del id. Para 'ollama:foo' se usa `curl` al endpoint."""


# ---------------------------------------------------------------------------
# Tier 0 — Escáner (LOCAL, gratis, alta concurrencia).
# Idea: el mayor número de hipótesis al menor coste, luego filtrar arriba.
# ---------------------------------------------------------------------------

TIER0_HUNTERS: tuple[ModelSpec, ...] = (
    ModelSpec(
        id='ollama:qwen3.5:9b',
        family='qwen',
        role='local',
        relative_cost=1,
        context_window=32_000,
        strengths=('code', 'fast'),
    ),
)

TIER0_30B_OPINIONS: tuple[ModelSpec, ...] = (
    ModelSpec(
        id='ollama:qwen3-coder:30b',
        family='qwen',
        role='local',
        relative_cost=2,
        context_window=32_000,
        strengths=('code', 'reasoning'),
    ),
)

# ---------------------------------------------------------------------------
# Tier 1 — Filtro (CHICO CLOUD, alta cuota, media concurrencia).
# Filtra las hipótesis del escáner local. Diversidad de familias = 합의 señal.
# ---------------------------------------------------------------------------

TIER1_FILTERS: tuple[ModelSpec, ...] = (
    # Minimax-M3 (yo) primero: alta cuota, bueno, barato, integrado.
    ModelSpec(
        id='minimax:MiniMax-M3',
        family='minimax',
        role='cloud_mini',
        relative_cost=2,
        context_window=1_000_000,
        strengths=('code', 'reasoning', 'long', 'json'),
    ),
    # Gemini 2.5 Flash: alta cuota, rápido.
    ModelSpec(
        id='gemini:gemini-2.5-flash',
        family='gemini',
        role='cloud_mini',
        relative_cost=2,
        context_window=1_000_000,
        strengths=('code', 'fast', 'json', 'long'),
    ),
    # Gemini 2.5 Flash Lite: aún más barato, más cuota.
    ModelSpec(
        id='gemini:gemini-2.5-flash-lite',
        family='gemini',
        role='cloud_mini',
        relative_cost=1,
        context_window=1_000_000,
        strengths=('code', 'fast', 'json'),
    ),
    # Claude Haiku: tier bajo de Anthropic.
    ModelSpec(
        id='claude:claude-haiku-4-5',
        family='claude',
        role='cloud_mini',
        relative_cost=3,
        context_window=200_000,
        strengths=('code', 'fast', 'json'),
    ),
)

# ---------------------------------------------------------------------------
# Tier 2 — Jueces (MEDIO FUERTE, baja frecuencia).
# REAL vs NOT_A_BUG. Quorum 2/3 o 2/4 según cuántos respondan.
# ---------------------------------------------------------------------------

TIER2_JUDGES: tuple[ModelSpec, ...] = (
    # Minimax-M3 primero (barato, capaz, alta cuota).
    ModelSpec(
        id='minimax:MiniMax-M3',
        family='minimax',
        role='cloud_mid',
        relative_cost=2,
        context_window=1_000_000,
        strengths=('code', 'reasoning', 'long', 'json'),
    ),
    # Codex (OpenAI): muy bueno para código, cadencia rápida.
    ModelSpec(
        id='codex:gpt-5-codex',
        family='codex',
        role='cloud_mid',
        relative_cost=4,
        context_window=400_000,
        strengths=('code', 'fix', 'reasoning'),
    ),
    # Gemini 2.5 Pro: buen razonador.
    ModelSpec(
        id='gemini:gemini-2.5-pro',
        family='gemini',
        role='cloud_mid',
        relative_cost=4,
        context_window=2_000_000,
        strengths=('code', 'reasoning', 'long'),
    ),
    # Claude Sonnet 4.6: el "default" para código sólido. ÚLTIMO en el juez
    # para preservar su cuota semanal (es el más caro y el más limitado).
    ModelSpec(
        id='claude:claude-sonnet-4-6',
        family='claude',
        role='cloud_mid',
        relative_cost=5,
        context_window=200_000,
        strengths=('code', 'reasoning', 'json', 'long'),
    ),
)

# ---------------------------------------------------------------------------
# Tier 3 — Fixer (FUERTE, raro).
# Cadena con fallback: codex → minimax → sonnet → gemini-pro → local-30b.
# Si TODOS los cloud fallan → local 30b (gratis, sin contención con 9b si
# Ollama tiene max_loaded_models >= 2).
# ---------------------------------------------------------------------------

TIER3_FIXERS: tuple[ModelSpec, ...] = (
    ModelSpec(
        id='codex:gpt-5-codex',
        family='codex',
        role='cloud_mid',
        relative_cost=4,
        context_window=400_000,
        strengths=('code', 'fix', 'reasoning'),
    ),
    ModelSpec(
        id='minimax:MiniMax-M3',
        family='minimax',
        role='cloud_mid',
        relative_cost=2,
        context_window=1_000_000,
        strengths=('code', 'fix', 'reasoning', 'long'),
    ),
    ModelSpec(
        id='gemini:gemini-2.5-pro',
        family='gemini',
        role='cloud_mid',
        relative_cost=4,
        context_window=2_000_000,
        strengths=('code', 'fix', 'long'),
    ),
    ModelSpec(
        id='claude:claude-sonnet-4-6',
        family='claude',
        role='cloud_mid',
        relative_cost=5,
        context_window=200_000,
        strengths=('code', 'fix', 'reasoning'),
    ),
    # Local 30b como último recurso (gratis; requiere OLLAMA_MAX_LOADED_MODELS>=2).
    ModelSpec(
        id='ollama:qwen3-coder:30b',
        family='local_qwen',
        role='local',
        relative_cost=2,
        context_window=32_000,
        strengths=('code', 'fix', 'reasoning'),
    ),
)

# ---------------------------------------------------------------------------
# Tier 4 — Árbitro (RARÍSIMO, solo riesgosos + discordia o agotamiento).
# Política Opus:
#   1. Se invoca SOLO si el fix toca dinero, stock, estado, o auth.
#   2. Se invoca SOLO si los 3 árbitros baratos discordaron o están en cuarentena.
#   3. Se imprime un banner de aviso cada vez.
# ---------------------------------------------------------------------------

TIER4_ARBITERS: tuple[ModelSpec, ...] = (
    ModelSpec(
        id='claude:claude-sonnet-4-6',
        family='claude',
        role='cloud_mid',
        relative_cost=5,
        context_window=200_000,
        strengths=('code', 'reasoning', 'json'),
    ),
    ModelSpec(
        id='minimax:MiniMax-M3',
        family='minimax',
        role='cloud_mid',
        relative_cost=2,
        context_window=1_000_000,
        strengths=('code', 'reasoning', 'long'),
    ),
    ModelSpec(
        id='gemini:gemini-2.5-pro',
        family='gemini',
        role='cloud_mid',
        relative_cost=4,
        context_window=2_000_000,
        strengths=('code', 'reasoning', 'long'),
    ),
)

# Opus SOLO como último recurso absoluto, fuera de la cadena normal.
# El planificador lo inserta al final de `TIER4_ARBITERS` si se cumplen
# las 3 condiciones de la política. Se modela aparte para que la decisión
# de activarlo sea explícita y visible en logs.
TIER4_OPUS_LAST_RESORT = ModelSpec(
    id='claude:claude-opus-4-6',
    family='claude',
    role='cloud_big',
    relative_cost=10,
    context_window=200_000,
    strengths=('code', 'reasoning', 'json'),
)


# ---------------------------------------------------------------------------
# Patrones "fix riesgoso" — el árbitro los mira más de cerca.
# Si el fix toca una ruta de dinero, stock, cambio de estado, o auth,
# requiere tier-4 activo y un banner de aviso.
# ---------------------------------------------------------------------------

RISKY_PATH_PATTERNS: tuple[str, ...] = (
    # Dinero / cartera
    r'/(cobros|pagos|facturas|caja|cartera|descuento)/',
    # Stock / inventario
    r'/(inventario|stock|orders?)/',
    # Estado de pedidos / despacho
    r'/(despacho|ruta|entrega|pedido)/',
    # Auth / permisos
    r'/(auth|signin|sign-in|usuarios|admin)/',
)


def is_risky_path(path: str) -> bool:
    """True si la ruta toca dinero, stock, estado, o auth."""
    import re
    return any(re.search(p, path) for p in RISKY_PATH_PATTERNS)


# ---------------------------------------------------------------------------
# Cadencia objetivo por tier (sugerencias para el planificador).
# ---------------------------------------------------------------------------

TARGET_CADENCE = {
    'tier0_scanner_per_min': 30,    # 30 hipótesis/minuto (4-6 hilos 9b).
    'tier1_filter_per_min': 10,     # 10 filtrados/minuto (chico cloud).
    'tier2_judge_per_min': 4,       # 4 fallos reales para confirmar/hora.
    'tier3_fix_per_min': 0.5,       # 1 fix cada 2 minutos cuando hay backlog.
    'tier4_arbiter_per_min': 0.05,  # 1 árbitro cada 20 min (raro).
}


def get_all_specs() -> list[ModelSpec]:
    """Todos los ModelSpec conocidos (para `ai-usage.sh` budget planning)."""
    out: list[ModelSpec] = []
    for tier in (TIER0_HUNTERS, TIER0_30B_OPINIONS, TIER1_FILTERS, TIER2_JUDGES,
                 TIER3_FIXERS, TIER4_ARBITERS, [TIER4_OPUS_LAST_RESORT]):
        out.extend(tier)
    return out


# ---------------------------------------------------------------------------
# CLI / invocación: el `command_template` se traduce a un comando real.
# ---------------------------------------------------------------------------

def resolve_command(spec: ModelSpec, prompt: str, system: str = '') -> list[str]:
    """Convierte un `ModelSpec` + prompt en un comando argv ejecutable."""
    if spec.id.startswith('ollama:'):
        # HTTP a Ollama; el caller debe usar el cliente HTTP, no subprocess.
        raise ValueError('Ollama models se invocan vía HTTP, no subprocess.')
    if spec.id.startswith('minimax:'):
        # El harness opencode inyecta Minimax automáticamente. Devolvemos un marcador.
        return ['opencode', '--model', spec.id.split(':', 1)[1], prompt]
    if spec.id.startswith('gemini:'):
        return ['gemini', '-m', spec.id.split(':', 1)[1], '-p', prompt]
    if spec.id.startswith('claude:'):
        # Claude Code CLI: `claude -p "<prompt>" --model <id>`.
        return ['claude', '-p', prompt, '--model', spec.id.split(':', 1)[1]]
    if spec.id.startswith('codex:'):
        return ['codex', '--model', spec.id.split(':', 1)[1], prompt]
    raise ValueError(f'Unknown model id: {spec.id}')


# ---------------------------------------------------------------------------
# Override por env vars. Cada tier puede sobrescribirse completamente.
# Formato: `LOOP_<TIER>=id1,id2,id3` separado por comas. El primero es el
# preferido; los siguientes son fallbacks en orden.
# ---------------------------------------------------------------------------

def _load_override(env_var: str, default: tuple[ModelSpec, ...]) -> tuple[ModelSpec, ...]:
    raw = os.environ.get(env_var)
    if not raw:
        return default
    ids = [s.strip() for s in raw.split(',') if s.strip()]
    catalog = {m.id: m for m in get_all_specs()}
    out: list[ModelSpec] = []
    for i in ids:
        if i in catalog:
            out.append(catalog[i])
        else:
            # Permitir IDs nuevos (ej. "minimax:MiniMax-M4-future") como spec ad-hoc.
            out.append(
                ModelSpec(
                    id=i,
                    family=i.split(':', 1)[0],
                    role='cloud_mid' if 'claude' in i or 'minimax' in i else 'cloud_mini',
                    relative_cost=5,
                    strengths=('code',),
                )
            )
    return tuple(out) if out else default


def get_tier0_hunters() -> tuple[ModelSpec, ...]:
    return _load_override('LOOP_TIER0_HUNTERS', TIER0_HUNTERS)


def get_tier0_30b() -> tuple[ModelSpec, ...]:
    return _load_override('LOOP_TIER0_30B', TIER0_30B_OPINIONS)


def get_tier1_filters() -> tuple[ModelSpec, ...]:
    return _load_override('LOOP_TIER1_FILTERS', TIER1_FILTERS)


def get_tier2_judges() -> tuple[ModelSpec, ...]:
    return _load_override('LOOP_TIER2_JUDGES', TIER2_JUDGES)


def get_tier3_fixers() -> tuple[ModelSpec, ...]:
    return _load_override('LOOP_TIER3_FIXERS', TIER3_FIXERS)


def get_tier4_arbiters(allow_opus: bool = False) -> tuple[ModelSpec, ...]:
    base = _load_override('LOOP_TIER4_ARBITERS', TIER4_ARBITERS)
    if allow_opus:
        # Política: Opus al final SOLO si el caller lo pide explícitamente.
        return (*base, TIER4_OPUS_LAST_RESORT)
    return base
