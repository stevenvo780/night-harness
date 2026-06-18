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
#   cloud_mini  — modelos cloud baratos y rápidos (Minimax, Haiku, Flash Lite, opencode free)
#   cloud_mid   — modelos cloud medios (Flash, Sonnet, Pro, opencode-go pro)
#   cloud_big   — modelos cloud grandes (Pro-thinking, Opus, opencode-go max)
#
# Familias (para diversidad en consenso):
#   qwen, minimax, gemini, claude, codex, local_qwen, opencode, opencode_go
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
    base_url: Optional[str] = None
    """Base URL para modelos Ollama. None => LOOP_OLLAMA_BASE."""
    gpu_index: Optional[int] = None
    """GPU a pinear para este modelo."""
    gpu_uuid: Optional[str] = None
    """UUID de la GPU (para identificación dinámica)."""
    node: Optional[str] = None
    """Nombre del nodo donde corre este modelo."""
    num_ctx: Optional[int] = None
    """Contexto a pedir a Ollama (ajustado por VRAM)."""


# ---------------------------------------------------------------------------
# Tier 0 — Escáner (LOCAL, gratis, alta concurrencia).
# Idea: el mayor número de hipótesis al menor coste, luego filtrar arriba.
# Los locales son ILIMITADOS (no consumen quota cloud) — la máquina es potente,
# quemamos GPU/CPU a full.
#
# ESTRATEGIA (jul-2026, run real): 2 modelos locales en PARALELO sin contención:
#   · LOCAL_GPU: 30B en RTX 5070 Ti (10.6GB VRAM, 1-3s por respuesta).
#   · LOCAL_CPU: 80B CPU explícito (32 cores ociosos, 30-60s por respuesta).
# Con 4 hunters 30B + 2 hunters 80B CPU = 6 hilos, sin contención entre
# GPU y CPU. La CPU tiene load 22/32 (24% idle) y la GPU 14% util → hay
# MUCHO margen para más cadencia.
# ---------------------------------------------------------------------------

TIER0_HUNTERS: tuple[ModelSpec, ...] = (
    # LOCAL_GPU: 30B, ~12.3GB VRAM residente. Hunter principal (1-3s por respuesta).
    # Hunters con índice 0, 2, 4, 6 (pares) usan este.
    ModelSpec(
        id='ollama:qwen3-coder:30b',
        family='qwen',
        role='local',
        relative_cost=2,
        context_window=32_000,
        strengths=('code', 'reasoning'),
    ),
    # LOCAL_CPU: 80B CPU explícito, no consume VRAM, 30-60s por respuesta.
    # Más razonador que el 30B. Hunters con índice 1, 3, 5, 7 (impares) usan este.
    ModelSpec(
        id='ollama:qwen3-coder-next:cpu',
        family='qwen',
        role='local',
        relative_cost=3,
        context_window=32_000,
        strengths=('code', 'reasoning', 'long'),
    ),
)

# Second opinion: el mismo 30B (consistente con la lección del post-mortem).
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

# Cloud hunters: modelos CHIQUITOS cloud (baratos, rápidos) que también cazan bugs.
# Multiplican la cadencia y la diversidad de perspectivas.
TIER0_CLOUD_HUNTERS: tuple[ModelSpec, ...] = (
    # Gemini Flash Lite: 1M ctx, $0.10/M, barato.
    ModelSpec(
        id='gemini:gemini-2.5-flash-lite',
        family='gemini',
        role='cloud_mini',
        relative_cost=1,
        context_window=1_000_000,
        strengths=('code', 'fast', 'long'),
    ),
    # DeepSeek Flash Free: free, rápido, vía opencode.
    ModelSpec(
        id='opencode:deepseek-v4-flash-free',
        family='opencode',
        role='cloud_mini',
        relative_cost=1,
        context_window=128_000,
        strengths=('code', 'fast', 'reasoning'),
    ),
    # North Mini Code Free: free, especializado en código.
    ModelSpec(
        id='opencode:north-mini-code-free',
        family='opencode',
        role='cloud_mini',
        relative_cost=1,
        context_window=128_000,
        strengths=('code', 'fast'),
    ),
    # Mimo Free: free, rápido.
    ModelSpec(
        id='opencode:mimo-v2.5-free',
        family='opencode',
        role='cloud_mini',
        relative_cost=1,
        context_window=128_000,
        strengths=('code', 'fast'),
    ),
    # Big Pickle Free.
    ModelSpec(
        id='opencode:big-pickle',
        family='opencode',
        role='cloud_mini',
        relative_cost=1,
        context_window=128_000,
        strengths=('code', 'fast'),
    ),
    # Minimax M2.7-highspeed: el más rápido/barato de minimax.
    ModelSpec(
        id='minimax/MiniMax-M2.7-highspeed',
        family='minimax',
        role='cloud_mini',
        relative_cost=1,
        context_window=1_000_000,
        strengths=('code', 'fast'),
    ),
    # Minimax M2.5-highspeed: otra alternativa rápida.
    ModelSpec(
        id='minimax/MiniMax-M2.5-highspeed',
        family='minimax',
        role='cloud_mini',
        relative_cost=1,
        context_window=1_000_000,
        strengths=('code', 'fast'),
    ),
    # Claude Haiku: rápido de Anthropic.
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
# Tier 1 — Filtro (CHICO CLOUD, alta cuota, media concurrencia).
# Filtra las hipótesis del escáner local. Diversidad de familias = 합의 señal.
# Maximizamos el paralelismo con VARIOS modelos cloud baratos en paralelo.
# Modelos verificados que SÍ responden en este entorno:
#   - minimax/MiniMax-M3 (alta cuota, 51M tokens/día)
#   - minimax/MiniMax-M2.5-highspeed (chico, rápido)
#   - minimax/MiniMax-M2.7-highspeed (chico, rápido)
#   - gemini-2.5-flash-lite (alta cuota 95%, test OK)
#   - gemini-3-flash-preview (preview, alta cuota)
#   - claude-haiku-4-5 (tier bajo Anthropic)
# ---------------------------------------------------------------------------

TIER1_FILTERS: tuple[ModelSpec, ...] = (
    # Minimax-M3 (yo) primero: alta cuota, barato, capaz.
    ModelSpec(
        id='minimax/MiniMax-M3',
        family='minimax',
        role='cloud_mini',
        relative_cost=2,
        context_window=1_000_000,
        strengths=('code', 'reasoning', 'long', 'json'),
    ),
    # Minimax chiquito y rápido (alternativa al M3).
    ModelSpec(
        id='minimax/MiniMax-M2.7-highspeed',
        family='minimax',
        role='cloud_mini',
        relative_cost=1,
        context_window=1_000_000,
        strengths=('code', 'fast'),
    ),
    # Minimax M2.5 highspeed (más estable que M2.7).
    ModelSpec(
        id='minimax/MiniMax-M2.5-highspeed',
        family='minimax',
        role='cloud_mini',
        relative_cost=1,
        context_window=1_000_000,
        strengths=('code', 'fast'),
    ),
    # Gemini 2.5 Flash Lite: confirmado que funciona (test OK con --yolo --output-format text).
    # Tier 1 PRINCIPAL gemini: alta cuota (95% diaria), rápido, barato.
    ModelSpec(
        id='gemini:gemini-2.5-flash-lite',
        family='gemini',
        role='cloud_mini',
        relative_cost=1,
        context_window=1_000_000,
        strengths=('code', 'fast', 'json'),
    ),
    # Gemini 3 Flash Preview: nueva versión, probablemente más cuota.
    ModelSpec(
        id='gemini:gemini-3-flash-preview',
        family='gemini',
        role='cloud_mini',
        relative_cost=2,
        context_window=1_000_000,
        strengths=('code', 'fast', 'json', 'long'),
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
    # NOTA: gemini-2.5-flash está rate-limited (429). NO lo usamos en tier 1.

    # OpenCode free tier (jul-2026, key opencode-go del usuario):
    # 5 modelos FREE ideales para filtros. Sin coste (rate-limited por el servicio
    # pero sin afectar quota del usuario). Respuesta en 4-24s en test real.
    ModelSpec(
        id='opencode:deepseek-v4-flash-free',
        family='opencode',
        role='cloud_mini',
        relative_cost=1,
        context_window=128_000,
        strengths=('code', 'fast', 'reasoning'),
    ),
    ModelSpec(
        id='opencode:north-mini-code-free',
        family='opencode',
        role='cloud_mini',
        relative_cost=1,
        context_window=128_000,
        strengths=('code', 'fast'),
    ),
    ModelSpec(
        id='opencode:mimo-v2.5-free',
        family='opencode',
        role='cloud_mini',
        relative_cost=1,
        context_window=128_000,
        strengths=('code', 'fast'),
    ),
    ModelSpec(
        id='opencode:big-pickle',
        family='opencode',
        role='cloud_mini',
        relative_cost=1,
        context_window=128_000,
        strengths=('code', 'fast'),
    ),
    ModelSpec(
        id='opencode:nemotron-3-ultra-free',
        family='opencode',
        role='cloud_mini',
        relative_cost=1,
        context_window=128_000,
        strengths=('code', 'reasoning'),
    ),
)

# ---------------------------------------------------------------------------
# Tier 2 — Jueces (MEDIO FUERTE, baja frecuencia).
# REAL vs NOT_A_BUG. Quorum 2/3 o 2/4 según cuántos respondan.
# Modelos verificados: minimax (OK), gemini-2.5-pro (asumido), sonnet (OK).
# ---------------------------------------------------------------------------

TIER2_JUDGES: tuple[ModelSpec, ...] = (
    # Minimax-M3 primero (barato, capaz, alta cuota).
    ModelSpec(
        id='minimax/MiniMax-M3',
        family='minimax',
        role='cloud_mid',
        relative_cost=2,
        context_window=1_000_000,
        strengths=('code', 'reasoning', 'long', 'json'),
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
    # Gemini 3 Pro Preview: tier más nuevo, más razonador.
    ModelSpec(
        id='gemini:gemini-3-pro-preview',
        family='gemini',
        role='cloud_mid',
        relative_cost=4,
        context_window=2_000_000,
        strengths=('code', 'reasoning', 'long'),
    ),
    # Codex (OpenAI): muy bueno para código, cadencia rápida.
    # Verificado: `codex exec` con default `gpt-5.5` funciona. `codex-mini` NO soportado.
    ModelSpec(
        id='codex:gpt-5.5',
        family='codex',
        role='cloud_mid',
        relative_cost=4,
        context_window=400_000,
        strengths=('code', 'fix', 'reasoning'),
    ),
    # Claude Sonnet: ÚLTIMO en el juez para preservar su cuota semanal.
    ModelSpec(
        id='claude:claude-sonnet-4-5',
        family='claude',
        role='cloud_mid',
        relative_cost=5,
        context_window=200_000,
        strengths=('code', 'reasoning', 'json', 'long'),
    ),
    # OpenCode-Go PRO (jul-2026, key opencode-go del usuario):
    # 3 modelos PRO para jueces. Calidad/precio competitivas vs Sonnet/Opus.
    # kimi-k2.7-code: Moonshot Kimi especializado en código.
    ModelSpec(
        id='opencode-go:kimi-k2.7-code',
        family='opencode_go',
        role='cloud_mid',
        relative_cost=3,
        context_window=200_000,
        strengths=('code', 'reasoning', 'fix'),
    ),
    # deepseek-v4-pro: razonador fuerte (DeepSeek v4 Pro).
    ModelSpec(
        id='opencode-go:deepseek-v4-pro',
        family='opencode_go',
        role='cloud_mid',
        relative_cost=3,
        context_window=200_000,
        strengths=('code', 'reasoning', 'long'),
    ),
    # glm-5.2: Z.ai GLM 5.2, 1M context, ideal para codebases grandes.
    ModelSpec(
        id='opencode-go:glm-5.2',
        family='opencode_go',
        role='cloud_mid',
        relative_cost=4,
        context_window=1_000_000,
        strengths=('code', 'reasoning', 'long'),
    ),
)

# ---------------------------------------------------------------------------
# Tier 3 — Fixer (FUERTE, raro).
# Cadena con fallback: codex → minimax → sonnet → gemini-pro → local-30b.
# Si TODOS los cloud fallan → local 30b (gratis, sin contención con 9b si
# Ollama tiene max_loaded_models >= 2).
# ---------------------------------------------------------------------------

TIER3_FIXERS: tuple[ModelSpec, ...] = (
    # Codex primero: muy bueno generando fixes de código.
    ModelSpec(
        id='codex:gpt-5.5',
        family='codex',
        role='cloud_mid',
        relative_cost=4,
        context_window=400_000,
        strengths=('code', 'fix', 'reasoning'),
    ),
    # Minimax-M3 segundo: barato, capaz, alta cuota.
    ModelSpec(
        id='minimax/MiniMax-M3',
        family='minimax',
        role='cloud_mid',
        relative_cost=2,
        context_window=1_000_000,
        strengths=('code', 'fix', 'reasoning', 'long'),
    ),
    # Gemini 2.5 Pro.
    ModelSpec(
        id='gemini:gemini-2.5-pro',
        family='gemini',
        role='cloud_mid',
        relative_cost=4,
        context_window=2_000_000,
        strengths=('code', 'fix', 'long'),
    ),
    # Claude Sonnet.
    ModelSpec(
        id='claude:claude-sonnet-4-5',
        family='claude',
        role='cloud_mid',
        relative_cost=5,
        context_window=200_000,
        strengths=('code', 'fix', 'reasoning'),
    ),
    # Local 30b como último recurso (gratis, sin contención con 9b).
    ModelSpec(
        id='ollama:qwen3-coder:30b',
        family='local_qwen',
        role='local',
        relative_cost=2,
        context_window=32_000,
        strengths=('code', 'fix', 'reasoning'),
    ),
    # OpenCode-Go PRO: los MEJORES para generar fixes de código.
    # qwen3.7-max: Alibaba Qwen 3.7 Max — top de gama, excelente en código.
    ModelSpec(
        id='opencode-go:qwen3.7-max',
        family='opencode_go',
        role='cloud_big',
        relative_cost=5,
        context_window=200_000,
        strengths=('code', 'fix', 'reasoning', 'long'),
    ),
    # deepseek-v4-pro: razonador fuerte, excelente en código.
    ModelSpec(
        id='opencode-go:deepseek-v4-pro',
        family='opencode_go',
        role='cloud_mid',
        relative_cost=3,
        context_window=200_000,
        strengths=('code', 'fix', 'reasoning'),
    ),
    # kimi-k2.7-code: Moonshot Kimi Code — especializado en código.
    ModelSpec(
        id='opencode-go:kimi-k2.7-code',
        family='opencode_go',
        role='cloud_mid',
        relative_cost=3,
        context_window=200_000,
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
    # El 30B GPU como árbitro local (más rápido que el 80B CPU).
    ModelSpec(
        id='ollama:qwen3-coder:30b',
        family='qwen',
        role='local',
        relative_cost=2,
        context_window=32_000,
        strengths=('code', 'reasoning'),
    ),
    ModelSpec(
        id='claude:claude-sonnet-4-5',
        family='claude',
        role='cloud_mid',
        relative_cost=5,
        context_window=200_000,
        strengths=('code', 'reasoning', 'json'),
    ),
    ModelSpec(
        id='minimax/MiniMax-M3',
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
    # OpenCode-Go PRO como árbitros fuertes.
    # qwen3.7-max: top de gama Alibaba.
    ModelSpec(
        id='opencode-go:qwen3.7-max',
        family='opencode_go',
        role='cloud_big',
        relative_cost=5,
        context_window=200_000,
        strengths=('code', 'reasoning', 'long', 'fix'),
    ),
    # glm-5.2: Z.ai 1M context — útil para árbitro con codebase grande.
    ModelSpec(
        id='opencode-go:glm-5.2',
        family='opencode_go',
        role='cloud_mid',
        relative_cost=4,
        context_window=1_000_000,
        strengths=('code', 'reasoning', 'long'),
    ),
    # kimi-k2.7-code: especializado en código.
    ModelSpec(
        id='opencode-go:kimi-k2.7-code',
        family='opencode_go',
        role='cloud_mid',
        relative_cost=3,
        context_window=200_000,
        strengths=('code', 'reasoning', 'fix'),
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
    if spec.id.startswith('opencode:') or spec.id.startswith('opencode-go:'):
        # OpenCode built-in providers (jul-2026, key opencode-go del usuario).
        # Formato: `opencode run --model <provider>/<model> -` lee de stdin.
        return ['opencode', 'run', '--model', spec.id, '-']
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


def get_tier0_cloud_hunters() -> tuple[ModelSpec, ...]:
    return _load_override('LOOP_TIER0_CLOUD', TIER0_CLOUD_HUNTERS)


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
