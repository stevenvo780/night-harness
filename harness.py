#!/usr/bin/env python3
"""
HARNESS v7 Demeter — auto-endurecimiento nocturno E2E multi-fase.

Phases A–D implementadas: pipeline correcto, VRAM local + fleet remoto,
multi-nodo coordinado, TUI schema v2. Modelos locales (Ollama) + cloud (CLIs).

Cambios respecto v6:
  · **Tier 0 paralelo**: 4-6 hilos del 30b GPU + N hilos del 30b CPU + cloud chiquitos
  · **Tier 1 filtro**: consenso/refutación Minimax + Gemini Flash + Haiku
  · **Tier 2 jueces**: quorum 2/3 Minimax + Codex + Gemini Pro + Sonnet
  · **Tier 3 fixer**: Codex → Minimax → Gemini Pro → Sonnet → local 30b
  · **Fleet discovery**: GPUs locales + nodos remotos, VRAM-aware routing
  · **Memory persistente**: falsos positivos filtrados entre sesiones
  · **Gate E2E dirigido**: valida la ruta exacta del bug, no solo /
  · **Cuarentena de salud**: modelos muertos o tóxicos quedan en espera
  · **Consensus hunter**: 3 muestras, 2/3 acuerdan antes de emitir

Aislamiento: worktree /tmp/demeter-loop (rama loop-nocturno). NO toca main
ni producción. El MERGE lo aprueba el humano en la mañana.

USO:
  # 1) Worktree aislado + dev server contra DB de test
  git worktree add -B night-harness /tmp/wt HEAD
  DATABASE_URL="postgres://localhost:5432/app_e2e" PORT=3011 npm run dev -- --port 3011 &

  # 2) Lanza el harness
  LOOP_WT=/tmp/wt LOOP_SUBDIR=apps/dashboard LOOP_DYNAMIC_ID=<id-real> \\
    LOOP_HOURS=8 python3 harness.py
"""
from __future__ import annotations

import argparse
import concurrent.futures
import itertools
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

# Módulos del v6.
import fleet
import memory
import orchestrator
import quota
import tiers
from loop_harness import discovery as harness_discovery
from loop_harness import gates as harness_gates
from loop_harness import health as harness_health
from loop_harness import model_calls as harness_calls
from loop_harness import parser as harness_parser
from loop_harness import review as harness_review
from loop_harness import domain as harness_domain
from loop_harness import status as harness_status
from loop_harness.domain import Bug, Suspect

STATS_LOCK = threading.Lock()
_ORCH_LOCK = threading.Lock()
REVIEW_LOCK = threading.Lock()

# Args del CLI, expuesto a nivel módulo para que worker threads (judge, fixer,
# e2e producer) puedan leer `args.dry_run` / `args.no_e2e` sin pasarlos por
# threading.Thread. `main()` muta este mismo namespace vía `parse_args(namespace=...)`.
args: argparse.Namespace = argparse.Namespace(dry_run=False, no_e2e=False)

# Fleet de hardware (GPUs/nodos + planificación por VRAM). Poblado en main().
FLEET = None

# ---------------------------------------------------------------------------
# Config (env vars; con defaults razonables).
# ---------------------------------------------------------------------------

WT          = Path(os.environ.get('LOOP_WT', '/tmp/demeter-loop'))
SUBDIR      = os.environ.get('LOOP_SUBDIR', 'apps/dashboard')
WT_SUBDIR   = WT / SUBDIR     # cwd canónico para CLIs agénticos y gates (e2e, tsc)
DEV_URL     = os.environ.get('LOOP_DEV_URL', 'http://localhost:3011')
STORAGE     = os.environ.get('LOOP_STORAGE_STATE', '')  # vacío = modo demo
DYN_ID      = os.environ.get('LOOP_DYNAMIC_ID', '')     # id sembrado para [id]
HOURS       = int(os.environ.get('LOOP_HOURS', '8'))
TARGET      = int(os.environ.get('LOOP_TARGET', '50'))   # rondas limpias para "endurecer"

# Concurrencia por tier.
HUNTER_HILOS       = int(os.environ.get('LOOP_HUNTERS', '4'))     # hilos locales
SECOND_HILOS       = int(os.environ.get('LOOP_SECOND_HILOS', '2'))  # hilos 30b
CLOUD_HUNTER_HILOS = int(os.environ.get('LOOP_CLOUD_HUNTERS', '6'))  # hilos cloud chiquitos
FILTER_HILOS       = int(os.environ.get('LOOP_FILTER_HILOS', '6'))  # tier 1
JUDGE_HILOS        = int(os.environ.get('LOOP_JUDGE_HILOS', '3'))   # tier 2

# Quorum del filtro (cuántos modelos deben coincidir para aceptar/refutar).
# 2 = quórum de 2 modelos por suspect (rápido, barato, suficiente para tier 1).
# 3 = quórum de 3 (más seguro, más caro). Subir si el filtro es muy ruidoso.
FILTER_QUORUM   = int(os.environ.get('LOOP_FILTER_QUORUM', '2'))
FIXER_HILOS     = int(os.environ.get('LOOP_FIXER_HILOS', '1'))   # tier 3

# Watchdog.
STALL_SECS  = int(os.environ.get('LOOP_STALL_SECS', '600'))  # sin avance → libera locales
LOG_FILE    = Path(os.environ.get('LOOP_LOG', '/tmp/loop-v6.log'))
STATUS_FILE = Path('/tmp/loop-v6-status.json')
REVIEW_FILE = Path('/tmp/loop-needs-review.json')

# Consenso tier 0.
CONSENSUS_N  = int(os.environ.get('LOOP_CONSENSUS', '3'))     # muestras para auto-consistencia

# Reglas de gating.
JUDGE_MIN    = int(os.environ.get('LOOP_JUDGE_MIN', '2'))      # votos REAL para confirmar
JUDGE_TOTAL  = int(os.environ.get('LOOP_JUDGE_TOTAL', '3'))   # jueces que se invocan

# Riesgo.
RISKY_KEYWORDS = (
    'dinero', 'pago', 'cartera', 'cobro', 'factura', 'caja',
    'stock', 'inventario', 'despacho', 'entrega', 'ruta',
    'auth', 'login', 'signin', 'sign-in', 'permiso', 'admin',
    'estado', 'transicion', 'orden',
)


# ---------------------------------------------------------------------------
# Colas (productor-consumidor).
# ---------------------------------------------------------------------------

suspectq: queue.Queue[Suspect] = queue.Queue(maxsize=200)
bugq: queue.Queue[Bug] = queue.Queue(maxsize=50)
# Cola de tareas E2E: cada item es (route, module_label, depth_hint).
# La llena el productor (orquestador + discover_zones + journeys).
# Los workers E2E consumen y reportan hallazgos a suspectq.
e2e_taskq: queue.Queue[tuple[str, str, int]] = queue.Queue(maxsize=400)


# ---------------------------------------------------------------------------
# Estadísticas globales del run.
# ---------------------------------------------------------------------------

STATS = {
    'rounds': 0,
    'clean_rounds_in_a_row': 0,
    'suspects_produced': 0,
    'suspects_refuted_by_filter': 0,
    'suspects_filtered_as_known_fp': 0,
    'bugs_confirmed': 0,
    'fixes_attempted': 0,
    'fixes_succeeded': 0,
    'fixes_reverted': 0,
    'fixes_to_review': 0,
    'e2e_findings': 0,
    'e2e_devserver_skips': 0,
    'opus_invocations': 0,
    'started_at': time.time(),
}

# Estado del orquestador (se actualiza en main).
_current_orch_prompts = []
_current_e2e_journeys = []


# ---------------------------------------------------------------------------
# Estado por-thread y status JSON (fachada compatible; implementación modular).
# ---------------------------------------------------------------------------
THREAD_STATES = harness_status.THREAD_STATES
THREAD_STATES_LOCK = harness_status.THREAD_STATES_LOCK


def update_thread_state(name: str, **kwargs) -> None:
    harness_status.update_thread_state(name, **kwargs)


def get_thread_states() -> list[dict]:
    return harness_status.get_thread_states()


def _build_roles() -> list:
    return harness_status.build_roles(
        stats=STATS,
        stats_lock=STATS_LOCK,
        quarantined_models=quarantined_models,
        hunter_hilos=HUNTER_HILOS,
        second_hilos=SECOND_HILOS,
        cloud_hunter_hilos=CLOUD_HUNTER_HILOS,
        filter_hilos=FILTER_HILOS,
        judge_hilos=JUDGE_HILOS,
        fixer_hilos=FIXER_HILOS,
    )


def _build_nodes() -> list:
    return harness_status.build_nodes()


def write_status() -> None:
    payload = harness_status.build_status_payload(
        pid=os.getpid(),
        stats=STATS,
        stats_lock=STATS_LOCK,
        orch_lock=_ORCH_LOCK,
        orch_prompts=_current_orch_prompts,
        e2e_journeys=_current_e2e_journeys,
        orchestrator_model=orchestrator.ORCHESTRATOR_MODEL,
        roles=_build_roles(),
        nodes=_build_nodes(),
        quota_snapshot=quota.local_spend(),
        memory_obj=MEM,
    )
    harness_status.write_status_file(STATUS_FILE, payload)


# ---------------------------------------------------------------------------
# Logging thread-safe.
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()


def log(msg: str) -> None:
    line = f'[{time.strftime("%H:%M:%S")}] {msg}'
    with _LOG_LOCK:
        try:
            with LOG_FILE.open('a') as f:
                f.write(line + '\n')
        except OSError:
            pass
        print(line, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Salud de modelos (cuarentena por timeouts/fallos repetidos).
# Independiente de la CUOTA (quota.py): un modelo puede tener cuota pero estar
# "zombi" — responde timeout sistemático (p.ej. varios `*-free` de opencode o
# gemini-*-flash-preview). Tras HEALTH_FAIL_THRESHOLD fallos seguidos entra en
# cuarentena QUARANTINE_SECS; cualquier respuesta válida lo resetea. La cuarentena
# expira sola → el modelo tiene otra oportunidad (auto-curación).
# ---------------------------------------------------------------------------
QUARANTINE_SECS       = int(os.environ.get('LOOP_QUARANTINE_SECS', '600'))
HEALTH_FAIL_THRESHOLD = int(os.environ.get('LOOP_HEALTH_FAIL_THRESHOLD', '3'))

# Semilla de cuarentena: modelos con deadrate≥0.5 medidos en vivo (spec §8.2).
# Se siembran al arranque para no quemar 180s×3 antes de que el umbral los pille.
# Recuperables (la cuarentena expira). Sobrescribible por env.
INITIAL_QUARANTINE = tuple(
    m.strip() for m in os.environ.get(
        'LOOP_INITIAL_QUARANTINE',
        'gemini:gemini-3-flash-preview,'
        'gemini:gemini-2.5-flash-lite,'
        'minimax/MiniMax-M2.7-highspeed,'
        'opencode:mimo-v2.5-free'
    ).split(',') if m.strip()
)

_HEALTH: dict[str, dict] = {}
_HEALTH_LOCK = threading.Lock()


def _mark_health(model_id: str, ok: bool) -> None:
    harness_health.mark_health(
        _HEALTH,
        _HEALTH_LOCK,
        model_id=model_id,
        ok=ok,
        fail_threshold=HEALTH_FAIL_THRESHOLD,
        quarantine_secs=QUARANTINE_SECS,
        log=log,
    )


def is_healthy(model_id: str) -> bool:
    return harness_health.is_healthy(_HEALTH, _HEALTH_LOCK, model_id)


def quarantined_models() -> list[str]:
    return harness_health.quarantined_models(_HEALTH, _HEALTH_LOCK)


def seed_quarantine(ids=INITIAL_QUARANTINE) -> None:
    harness_health.seed_quarantine(
        _HEALTH,
        _HEALTH_LOCK,
        ids,
        fail_threshold=HEALTH_FAIL_THRESHOLD,
        quarantine_secs=QUARANTINE_SECS,
        log=log,
    )


def health_probe_all(specs, timeout: int = 25, workers: int = 8) -> None:
    harness_health.health_probe_all(
        specs,
        call_model=call_model,
        health=_HEALTH,
        lock=_HEALTH_LOCK,
        fail_threshold=HEALTH_FAIL_THRESHOLD,
        quarantine_secs=QUARANTINE_SECS,
        log=log,
        timeout=timeout,
        workers=workers,
    )


# ---------------------------------------------------------------------------
# Invocación de modelos: fachada compatible; implementación en loop_harness.
# ---------------------------------------------------------------------------

_OLLAMA_SEMAPHORES = harness_calls._OLLAMA_SEMAPHORES
_OLLAMA_SEM_GUARD = harness_calls._OLLAMA_SEM_GUARD


def call_model(spec: tiers.ModelSpec, system: str, prompt: str, max_tokens: int = 2048,
               timeout: int = 120) -> Optional[str]:
    return harness_calls.call_model(
        spec,
        system,
        prompt,
        max_tokens=max_tokens,
        timeout=timeout,
        quota_module=quota,
        is_healthy=is_healthy,
        mark_health=_mark_health,
        log=log,
        call_ollama=_call_ollama,
        call_minimax=_call_minimax,
        call_opencode=_call_opencode,
        call_gemini_cli=_call_gemini_cli,
        call_claude_cli=_call_claude_cli,
        call_codex_cli=_call_codex_cli,
    )


def _cli_cwd() -> Optional[str]:
    return harness_calls.cli_cwd(WT_SUBDIR)


def _is_cpu_model(model_name: str) -> bool:
    return harness_calls.is_cpu_model(model_name)


def _ollama_sem(model_name: str) -> threading.Semaphore:
    return harness_calls.ollama_sem(model_name)


def _call_ollama(spec: tiers.ModelSpec, prompt: str, system: str, max_tokens: int,
                 timeout: int) -> str:
    return harness_calls.call_ollama(spec, prompt, system, max_tokens, timeout)


def _clean_cli_out(stdout: str) -> str:
    return harness_calls.clean_cli_out(stdout)


def _call_cli(cmd: list[str], prompt: str, system: str, timeout: int,
              *, name: str = 'cli',
              min_timeout: int = 0,
              on_stderr=None) -> str:
    return harness_calls.call_cli(
        cmd,
        prompt,
        system,
        timeout,
        cwd=_cli_cwd,
        log=log,
        name=name,
        min_timeout=min_timeout,
        on_stderr=on_stderr,
    )


def _call_minimax(spec: tiers.ModelSpec, prompt: str, system: str, max_tokens: int,
                  timeout: int) -> str:
    return harness_calls.call_minimax(
        spec, prompt, system, max_tokens, timeout, cwd=_cli_cwd, log=log
    )


def _call_minimax_http(model: str, prompt: str, max_tokens: int, timeout: int) -> str:
    return harness_calls.call_minimax_http(model, prompt, max_tokens, timeout)


def _call_opencode(spec: tiers.ModelSpec, prompt: str, system: str, max_tokens: int,
                   timeout: int) -> str:
    return harness_calls.call_opencode(
        spec, prompt, system, max_tokens, timeout, cwd=_cli_cwd, log=log
    )


def _call_claude_cli(spec: tiers.ModelSpec, prompt: str, system: str, max_tokens: int,
                     timeout: int) -> str:
    return harness_calls.call_claude_cli(
        spec, prompt, system, max_tokens, timeout, cwd=_cli_cwd, log=log
    )


def _call_gemini_cli(spec: tiers.ModelSpec, prompt: str, system: str, max_tokens: int,
                     timeout: int) -> str:
    return harness_calls.call_gemini_cli(
        spec, prompt, system, max_tokens, timeout, cwd=_cli_cwd, log=log
    )


def _call_codex_cli(spec: tiers.ModelSpec, prompt: str, system: str, max_tokens: int,
                    timeout: int) -> str:
    return harness_calls.call_codex_cli(
        spec, prompt, system, max_tokens, timeout, cwd=_cli_cwd, log=log
    )


# ---------------------------------------------------------------------------
# Tier 0 — Hunters (escáner local de código).
# Estrategia: el hunter PRODUCE, el filtro CONSUME. Múltiples hilos para
# cadencia alta. Cada hunter mira una zona y emite suspectq.
# ---------------------------------------------------------------------------

HUNTER_SYSTEM = """Eres un auditor de código TypeScript/Next.js. Tu trabajo es
encontrar BUGS REALES en el código. NO reportes:
  · Estilo, naming, o convenciones.
  · Mejoras de performance sin impacto funcional.
  · Sugerencias de refactor sin bug concreto.
  · Falsos positivos que ya conoces.

SÍ reporta (con archivo:línea:snippet:razonamiento):
  · Validación faltante o incorrecta (zod, auth, tenant-scope).
  · Race conditions o state mutations no atómicas.
  · Errores de tipo o de runtime que se manifiestan en casos reales.
  · Lógica que falla en condiciones de borde documentadas.
  · Multi-tenant leaks (data de otro tenant).
  · Inyecciones, XSS, SSRF, IDOR.
  · Fallos de transacciones DB (sin rollback, sin atomicidad).
  · Stocks/cartera/dinero que se pueden corromper.

FORMATO DE SALIDA: una línea JSON por bug (JSONL), nada más antes ni después.
{"file":"lib/queries/clientes.ts","line":42,"severity":"high","snippet":"const x = await db.query(...)","why":"falta filtro tenantId","repro":"abrir /clientes con otro tenant"}

Donde:
  - file: ruta relativa al subdir del repo
  - line: entero (número de línea aproximado)
  - severity: low | medium | high | critical
  - snippet: el CÓDIGO sospechoso LITERAL (NO tu explicación)
  - why: 1-2 frases explicando por qué es bug
  - repro: cómo dispararlo (opcional)

Una línea JSON por bug. Si NO encuentras bugs en la zona, responde EXACTAMENTE: NO_BUGS
"""


def _parse_bug_line(line: str) -> Optional[dict]:
    return harness_parser.parse_bug_line(line)


def hunter_worker(zone_files: list[str], hunter_spec: tiers.ModelSpec, wid: int,
                 hunting_prompts: Optional[list] = None) -> None:
    """Un hilo del escáner. Procesa una zona y emite suspectq.

    Si `hunting_prompts` está presente (generado por el orquestador), cada
    archivo recibe prompts DIRIGIDOS con conocimiento del dominio (estados,
    auth, relaciones), no genéricos. Esto multiplica la tasa de bugs reales.
    """
    # Indexar prompts por archivo para lookup rápido.
    prompts_by_file: dict[str, list] = {}
    if hunting_prompts:
        for p in hunting_prompts:
            fname = p.file if hasattr(p, 'file') else p.get('file', '')
            prompts_by_file.setdefault(fname, []).append(p)

    # Determinar source: local (ruidoso) vs cloud (preciso).
    # Cloud hunters saltan el filtro y van directo a jueces.
    is_cloud = not hunter_spec.id.startswith('ollama:')
    source_kind = 'cloud' if is_cloud else 'local'

    log(f'  [hunter-{wid}] {hunter_spec.id} arrancando ({len(zone_files)} archivos'
        f'{", orquestador activo" if hunting_prompts else ""}, source={source_kind})')
    node_name = getattr(hunter_spec, 'node', 'local')
    update_thread_state(f'hunter-{wid}', kind=('cloud' if is_cloud else 'hunter'),
                        role='hunter', location=node_name, node=node_name,
                        model=hunter_spec.id,
                        status='busy', current_file='', calls=0)
    for file in itertools.cycle(zone_files):
        if shutdown.is_set():
            break
        path = WT / SUBDIR / file
        if not path.exists():
            continue
        try:
            code = path.read_text(errors='ignore')
        except OSError:
            continue
        if not code.strip():
            continue

        # Determinar si hay prompts de orquestador para este archivo.
        targeted = prompts_by_file.get(file, []) if hunting_prompts else []
        update_thread_state(f'hunter-{wid}', role='hunter', location=node_name, node=node_name,
                            current_file=file, status='busy',
                            current_task=f'revisando ({len(targeted)} pistas)' if targeted else 'revisando')

        # Consenso: N muestras; solo emitimos si 2/3 coinciden en (file, line, snippet).
        _consensus_collector = {}
        required_votes = max(1, (2 * CONSENSUS_N + 2) // 3)
        for round_n in range(CONSENSUS_N):
            if targeted:
                # PROMPT DIRIGIDO: el orquestador nos dice QUÉ buscar en este archivo.
                ctx_lines = []
                for tp in targeted[:3]:  # máximo 3 prompts por archivo
                    reason = tp.reason if hasattr(tp, 'reason') else tp.get('reason', '')
                    more_ctx = tp.context if hasattr(tp, 'context') else tp.get('context', '')
                    ctx_lines.append(f'  · {reason}')
                    if more_ctx:
                        ctx_lines.append(f'    Contexto: {more_ctx}')
                context_block = '\n'.join(ctx_lines)
                prompt = (
                    f'Pistas del orquestador para el archivo `{file}`:\n'
                    f'{context_block}\n\n'
                    f'```typescript\n{code[:8000]}\n```\n'
                    'Reporta CADA bug como UNA línea JSON (formato del sistema): '
                    '{"file":...,"line":...,"severity":...,"snippet":"<código literal>","why":"<razón>"}. '
                    'Si no hay bugs: NO_BUGS.\n'
                    'Céntrate SOLO en las pistas del orquestador.'
                )
            else:
                # PROMPT GENÉRICO (fallback si el orquestador no conoce este archivo).
                prompt = (
                    f'Audita el archivo `{file}` (longitud {len(code)} chars).\n'
                    f'```typescript\n{code[:8000]}\n```\n'
                    'Reporta CADA bug como UNA línea JSON (formato del sistema): '
                    '{"file":...,"line":...,"severity":...,"snippet":"<código literal>","why":"<razón>"}. '
                    'Si no hay bugs: NO_BUGS.\n'
                )
            update_thread_state(f'hunter-{wid}', current_task=f'round {round_n+1}/{CONSENSUS_N}')
            text = call_model(hunter_spec, HUNTER_SYSTEM, prompt, max_tokens=1500)
            if not text or 'NO_BUGS' in text:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith('NO_BUGS') or line.startswith('#'):
                    continue
                m = _parse_bug_line(line)
                if m:
                    key = (m['file'], m['line'])
                    entry = _consensus_collector.setdefault(key, {
                        'rounds': {},
                        'total_votes': {},
                        'snippet': '',
                        'why': '',
                        'severity': m.get('severity', 'medium'),
                        'repro': m.get('repro', ''),
                    })
                    snippet = (m.get('snippet') or '').strip()
                    round_snippets = entry['rounds'].setdefault(round_n, [])
                    if snippet not in round_snippets:
                        round_snippets.append(snippet)
                        entry['total_votes'][snippet] = entry['total_votes'].get(snippet, 0) + 1
                    if len(snippet) > len(entry['snippet']):
                        entry['snippet'] = snippet
                    why = (m.get('why') or '').strip()
                    if len(why) > len(entry['why']):
                        entry['why'] = why
                    if m.get('repro'):
                        entry['repro'] = m['repro']
                    entry['severity'] = m.get('severity', entry['severity'])
        for (cons_file, cons_line), entry in _consensus_collector.items():
            same_votes = max(entry['total_votes'].values(), default=0)
            if same_votes < required_votes:
                continue
            snippets = [
                snippet
                for round_snippets in entry['rounds'].values()
                for snippet in round_snippets
            ]
            combined_snippet = max(snippets, key=len, default=entry['snippet'])
            yield_suspect(
                Suspect(
                    file=cons_file,
                    line=cons_line,
                    snippet=combined_snippet,
                    why=entry['why'],
                    severity=entry['severity'],
                    repro=entry['repro'],
                    samples=same_votes,
                    source=source_kind,
                )
            )
        log(f'  [hunter-{wid}] {file} revisado')
        update_thread_state(f'hunter-{wid}', calls=THREAD_STATES.get(f'hunter-{wid}', {}).get('calls', 0) + 1)

    log(f'  [hunter-{wid}] {hunter_spec.id} done')
    update_thread_state(f'hunter-{wid}', status='done', current_task='terminado')


def yield_suspect(s: Suspect) -> None:
    """Encolar un suspect, con dedupe en memoria.

    RUTEO INTELIGENTE (jul-2026):
      · source='local' (Ollama, ruidoso) → suspectq → filtro → jueces
      · source='cloud' (gemini, opencode, minimax) → bugq → jueces directo
        Los modelos cloud son más precisos; saltan el filtro escéptico que
        siempre dice NOT_A_BUG y bloquea bugs reales.
    """
    # Anti-fantasmas: descartar suspects cuyo archivo no existe en el worktree.
    # El parser viejo producía paths basura (`fecha`, snippets como path) que
    # llegaban hasta el fixer y gastaban jueces antes de abortar. Los hallazgos
    # E2E se mapean a archivo real antes de entrar aquí.
    s.file = (s.file or '').strip().lstrip('`').lstrip('./')
    # Contención: la salida del modelo no debe escapar el worktree (path traversal).
    safe = harness_domain.safe_subpath(WT_SUBDIR, s.file)
    if safe is None or not safe.is_file():
        log(f'  [yield] descartado fantasma {s.file!r}:{s.line}')
        return

    if MEM.should_skip(s.file, s.line, s.snippet):
        with STATS_LOCK:
            STATS['suspects_filtered_as_known_fp'] += 1
        return
    if MEM.get_bug(s.file, s.line, s.snippet) and MEM.get_bug(s.file, s.line, s.snippet).fixed:
        return

    with STATS_LOCK:
        STATS['suspects_produced'] += 1
    try:
        suspectq.put_nowait(s)
    except queue.Full:
        pass


# ---------------------------------------------------------------------------
# Tier 1 — Filtro escéptico.
# Recibe suspectq, intenta REFUTAR cada hipótesis. Si NO puede, lo pasa a
# bugq para que los jueces lo confirmen.
# ---------------------------------------------------------------------------

FILTER_SYSTEM = """Eres un revisor escéptico. Recibes una hipótesis de bug y debes
decir si es REAL o NOT_A_BUG.

Reglas:
  · NO_BUG si el código YA tiene guards, validaciones, try/catch, o
    transacciones que el reporte ignora.
  · NO_BUG si el "bug" es un patrón intencional documentado o estándar
    de la industria.
  · NO_BUG si la corrección propuesta rompería otra cosa.
  · REAL si la hipótesis es válida, reproducible, y no está mitigada.

Responde SOLO:
  REAL  — si el bug es real y reproducible
  NOT_A_BUG — si el código ya lo maneja o el reporte es incorrecto

En una segunda línea, una frase corta justificando.
"""


def filter_worker(filters: list[tiers.ModelSpec], wid: int) -> None:
    """Un hilo del filtro. Consume suspectq, filtra escépticamente con CONSENSO.

    Política de consenso NO-BLOQUEANTE (jul-2026, fix paralelismo):
      · Antes: loop secuencial (esperaba cada modelo uno tras otro). Tiempo total
        = sum(t_model_i). Con 2 modelos de 15s cada uno → 30s por suspect.
      · Ahora: ThreadPoolExecutor con FILTER_QUORUM+1 workers en paralelo.
        Tiempo total = max(t_model_i) hasta tener quorum válido. Con 2 modelos
        de 15s cada uno → 15s por suspect. ~DOBLA el throughput.
      · Si un modelo falla/timeout, el futuro retorna ('FAIL', spec_id); los
        demás siguen contando.
      · Round-robin: cada wid arranca con un offset distinto; rota por suspect
        para que todos los modelos reciban tráfico equilibrado.
    """
    log(f'  [filter-{wid}] arrancando con {[f.id for f in filters]} (quorum={FILTER_QUORUM})')
    n = len(filters)
    offset = wid % n  # offset inicial distinto por hilo (diversidad)
    update_thread_state(f'filter-{wid}', kind='filter', model=filters[offset].id if filters else '?',
                        status='idle', current_file='', calls=0)
    while True:
        try:
            s = suspectq.get(timeout=5)
        except queue.Empty:
            if shutdown.is_set() and suspectq.empty():
                return
            continue

        prompt = (
            f'Archivo: {s.file}:{s.line}\n'
            f'Snippet: {s.snippet}\n'
            f'Razón: {s.why}\n\n'
            '¿Es REAL o NOT_A_BUG?'
        )
        # Round-robin: para cada suspect, rotamos qué modelo arranca.
        # Así un suspect lo ve (filters[offset], filters[offset+1], ...)
        # y el siguiente suspect empieza en offset+1.
        ordered = [filters[(offset + i) % n] for i in range(min(FILTER_QUORUM + 1, n))]
        offset = (offset + 1) % n

        update_thread_state(f'filter-{wid}', role='filter', location='cloud', node='cloud',
                            current_file=s.file, status='busy',
                            current_task=f'consenso {len(ordered)} modelos')

        # CONSENSO NO-BLOQUEANTE: disparar todos los modelos en paralelo.
        # Tiempo total = max(t_model_i) en lugar de sum(t_model_i).
        def _query(spec: tiers.ModelSpec) -> tuple[str, str]:
            text = call_model(spec, FILTER_SYSTEM, prompt, max_tokens=300, timeout=60)
            if not text:
                return ('FAIL', spec.id)
            head = text.strip().splitlines()[0].strip().upper()
            if 'REAL' in head and 'NOT' not in head:
                return ('REAL', spec.id)
            elif 'NOT_A_BUG' in head or 'NOT A BUG' in head:
                return ('NOT_A_BUG', spec.id)
            else:
                return ('UNCLEAR', spec.id)

        votes: list[tuple[str, str]] = []
        specs_used: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(ordered)) as pool:
            futures = {pool.submit(_query, spec): spec for spec in ordered}
            # Recoger resultados conforme llegan. Salimos cuando tenemos
            # FILTER_QUORUM votos válidos.
            for fut in concurrent.futures.as_completed(futures, timeout=120):
                try:
                    verdict, spec_id = fut.result(timeout=0.5)
                except Exception:
                    continue
                votes.append((verdict, spec_id))
                specs_used.append(spec_id)
                valid_so_far = sum(1 for v in votes if v[0] != 'FAIL')
                if valid_so_far >= FILTER_QUORUM:
                    break

        # Contar votos (excluyendo fallos).
        valid_votes = [v for v in votes if v[0] != 'FAIL']
        if len(valid_votes) < FILTER_QUORUM:
            # Menos de FILTER_QUORUM votos válidos: skip silencioso.
            update_thread_state(f'filter-{wid}', current_file='', status='idle', current_task='')
            suspectq.task_done()
            continue

        real_count = sum(1 for v in valid_votes if v[0] == 'REAL')
        notbug_count = sum(1 for v in valid_votes if v[0] == 'NOT_A_BUG')
        unclear_count = sum(1 for v in valid_votes if v[0] == 'UNCLEAR')
        specs_used_str = ','.join(s.split('/')[-1][:20] for s in specs_used[:FILTER_QUORUM])

        if real_count >= FILTER_QUORUM:
            # Quorum a favor de REAL: pasa a bugq (jueces lo confirmarán en tier 2).
            try:
                bugq.put_nowait(Bug(suspect=s))
                log(f'  [filter-{wid}] REAL [{real_count}R/{notbug_count}N/{unclear_count}U] '
                    f'{s.file}:{s.line} ({specs_used_str})')
            except queue.Full:
                pass
        elif notbug_count >= FILTER_QUORUM:
            # Quorum a favor de NOT_A_BUG: refutado.
            MEM.record_false_positive(s.file, s.line, s.snippet,
                                      reason=f'consenso {specs_used_str}')
            with STATS_LOCK:
                STATS['suspects_refuted_by_filter'] += 1
            log(f'  [filter-{wid}] NOT_A_BUG [{real_count}R/{notbug_count}N/{unclear_count}U] '
                f'{s.file}:{s.line} ({specs_used_str})')
        else:
            # Discordia: el filtro no decide. Lo dejamos pasar a tier 2 con
            # un flag "discordia" para que el juez pondere con más cuidado.
            try:
                b = Bug(suspect=s)
                # Marcamos discordia en el log; el juez leerá el contexto.
                log(f'  [filter-{wid}] DISCORD [{real_count}R/{notbug_count}N/{unclear_count}U] '
                    f'{s.file}:{s.line} → escalado a juez ({specs_used_str})')
                bugq.put_nowait(b)
            except queue.Full:
                pass

        # Actualizar contadores del thread.
        with THREAD_STATES_LOCK:
            t = THREAD_STATES.get(f'filter-{wid}', {})
            t['calls'] = t.get('calls', 0) + 1
        update_thread_state(f'filter-{wid}', current_file='', status='idle', current_task='')
        suspectq.task_done()


# ---------------------------------------------------------------------------
# Tier 2 — Jueces (REAL vs NOT_A_BUG con quorum).
# Reciben bugq (filtrados por tier 1), confirman o refutan.
# ---------------------------------------------------------------------------

JUDGE_SYSTEM = """Eres un juez de bugs. Recibes un reporte de bug y debes
evaluar:
  1. ¿Es REPRODUCIBLE? (¿el código realmente hace lo que el reporte dice?)
  2. ¿Es REAL? (¿no está mitigado por guards, validaciones, try/catch?)
  3. ¿Es un FALSO POSITIVO del hunter local? (¿patrón común que el 9b marca
     pero que el código maneja bien?)

Responde SOLO con uno de:
  REAL  — bug reproducible y no mitigado
  NOT_A_BUG  — código ya lo maneja, o el reporte es incorrecto
  WONT_FIX  — bug real pero no vale la pena arreglarlo ahora (cosmético, edge case raro)

Justificación en 1-2 frases.
"""


def judge_worker(judges: list[tiers.ModelSpec], wid: int) -> None:
    log(f'  [judge-{wid}] arrancando con {[j.id for j in judges]}')
    update_thread_state(f'judge-{wid}', kind='judge',
                        model=judges[0].id if judges else '?',
                        status='idle', current_file='', calls=0)
    while True:
        try:
            bug = bugq.get(timeout=5)
        except queue.Empty:
            if shutdown.is_set() and bugq.empty():
                update_thread_state(f'judge-{wid}', status='done')
                return
            continue

        prompt = (
            f'Bug: {bug.suspect.file}:{bug.suspect.line}\n'
            f'Snippet: {bug.suspect.snippet}\n'
            f'Razón original: {bug.suspect.why}\n\n'
            '¿Es REAL, NOT_A_BUG, o WONT_FIX?'
        )
        update_thread_state(f'judge-{wid}', role='judge', location='cloud', node='cloud',
                            current_file=bug.suspect.file,
                            status='busy', current_task=f'juzgando {len(judges)} modelos')

        # TAMBIÉN paralelo: lanzar todos los jueces en paralelo.
        def _judge_query(spec: tiers.ModelSpec) -> tuple[str, str]:
            text = call_model(spec, JUDGE_SYSTEM, prompt, max_tokens=400, timeout=60)
            if not text:
                return ('FAIL', spec.id)
            head = text.strip().splitlines()[0].strip().upper()
            if 'REAL' in head and 'NOT' not in head:
                return ('REAL', spec.id)
            elif 'NOT_A_BUG' in head or 'NOT A BUG' in head:
                return ('NOT_A_BUG', spec.id)
            else:
                return ('WONT_FIX', spec.id)  # WONT_FIX no suma votos

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(judges)) as pool:
            futures = {pool.submit(_judge_query, spec): spec for spec in judges}
            for fut in concurrent.futures.as_completed(futures, timeout=120):
                try:
                    verdict, spec_id = fut.result(timeout=0.5)
                except Exception:
                    continue
                bug.voters.append(spec_id)
                if verdict == 'REAL':
                    bug.votes_real += 1
                elif verdict == 'NOT_A_BUG':
                    bug.votes_not_bug += 1
                # WONT_FIX y FAIL no suman

        # Quorum.
        if bug.votes_real >= JUDGE_MIN and bug.votes_real > bug.votes_not_bug:
            with STATS_LOCK:
                STATS['bugs_confirmed'] += 1
            log(f'  [judge-{wid}] CONFIRMED {bug.suspect.file}:{bug.suspect.line} '
                f'({bug.votes_real}/{len(bug.voters)} REAL, sev={bug.suspect.severity})')
            # Compuerta de auto-fix agresivo: solo auto-arreglamos SIN revisión humana
            # si el bug es de severidad alta O hubo unanimidad de los jueces que votaron.
            # Lo confirmado-pero-dudoso (medium con voto dividido) va a revisión, no a
            # auto-commit ciego. Sobre-escribible con LOOP_AUTOFIX_ALL=1.
            unanimous = (bug.votes_not_bug == 0 and bug.votes_real == len(bug.voters)
                         and bug.votes_real >= JUDGE_MIN)
            high_sev = bug.suspect.severity in ('high', 'critical')
            if (os.environ.get('LOOP_AUTOFIX_ALL', '0') == '1' or high_sev or unanimous) and not args.dry_run:
                threading.Thread(
                    target=fixer_worker, args=(bug,), daemon=True, name=f'fixer-{wid}'
                ).start()
            else:
                with STATS_LOCK:
                    STATS['fixes_to_review'] += 1
                _add_to_review(bug.suspect, '', 'judge-confirmed-no-autofix')
                log(f'  [judge-{wid}] {bug.suspect.file}:{bug.suspect.line} confirmado '
                    f'(sev={bug.suspect.severity}, {bug.votes_real}R/{bug.votes_not_bug}N) '
                    f'→ revisión humana, no auto-fix')
        else:
            # No llegó a quorum → falso positivo probable.
            MEM.record_false_positive(bug.suspect.file, bug.suspect.line,
                                      bug.suspect.snippet,
                                      reason=f'{bug.votes_real}REAL/{bug.votes_not_bug}NOT')
        with THREAD_STATES_LOCK:
            t = THREAD_STATES.get(f'judge-{wid}', {})
            t['calls'] = t.get('calls', 0) + 1
        update_thread_state(f'judge-{wid}', current_file='', status='idle', current_task='')
        bugq.task_done()


# ---------------------------------------------------------------------------
# Tier 3 — Fixer (cadena con fallback).
# Aplica el fix, valida con tsc+E2E, commitea o revierte.
# ---------------------------------------------------------------------------

# Locks por archivo: evitan que 2+ fixers concurrentes editen el mismo archivo
# (leerían el mismo `original` y se pisarían el commit/corromperían el código).
_FILE_LOCKS: dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _file_lock(file: str) -> threading.Lock:
    with _FILE_LOCKS_GUARD:
        return _FILE_LOCKS.setdefault(file, threading.Lock())


def fixer_worker(bug: Bug) -> None:
    # Dedup: si el bug ya fue arreglado, no repetir.
    existing = MEM.get_bug(bug.suspect.file, bug.suspect.line, bug.suspect.snippet)
    if existing and getattr(existing, 'fixed', False):
        log(f'  [fixer] ya arreglado, skip {bug.suspect.file}:{bug.suspect.line}')
        return
    # Lock por archivo: un solo fixer a la vez por archivo (evita carrera).
    lock = _file_lock(bug.suspect.file)
    if not lock.acquire(blocking=False):
        log(f'  [fixer] {bug.suspect.file} en proceso por otro fixer, skip')
        return
    try:
        log(f'  [fixer] {bug.suspect.file}:{bug.suspect.line} arrancando')
        with STATS_LOCK:
            STATS['fixes_attempted'] += 1
        update_thread_state(f'fixer-{bug.suspect.file}', kind='fixer',
                            model='?', status='busy',
                            current_file=bug.suspect.file, calls=1)

        fixers = list(quota.filter_tier(tiers.get_tier3_fixers(), 'fixer'))
        # Fixer LOCAL del fleet primero (30B en GPU0, con base_url/gpu/num_ctx resueltos).
        if FLEET is not None and FLEET.fixer_spec() is not None:
            fx = FLEET.fixer_spec()
            fixers = [fx] + [f for f in fixers if f.id != fx.id]
        if not fixers:
            log('  [fixer] no hay fixers disponibles (todos en cuarentena)')
            update_thread_state(f'fixer-{bug.suspect.file}', status='done', current_task='sin fixers')
            return

        # Leer el código actual.
        path = WT / SUBDIR / bug.suspect.file
        if not path.exists():
            log(f'  [fixer] archivo no existe: {path}')
            return
        original = path.read_text(errors='ignore')

        for spec in fixers:
            prompt = (
                f'Bug confirmado: {bug.suspect.file}:{bug.suspect.line}\n'
                f'Snippet: {bug.suspect.snippet}\n'
                f'Razón: {bug.suspect.why}\n\n'
                f'Código actual (longitud {len(original)}):\n```typescript\n{original[:12000]}\n```\n'
                'Aplica el fix MÍNIMO necesario. Devuelve el archivo COMPLETO '
                'corregido, sin explicaciones. Si el fix requiere más de 200 líneas, '
                'describe solo el cambio y devuélvelo en un patch (diff unificado).'
            )
            # Update thread state con spec actual
            node_name = getattr(spec, 'node', 'local')
            update_thread_state(f'fixer-{bug.suspect.file}', model=spec.id,
                                role='fixer', location=node_name, node=node_name)
            # El fixer LOCAL (30B GPU) necesita timeout amplio (fix real ~123s)
            # y menos tokens para no excederse; el cloud usa los defaults.
            is_local = spec.id.startswith('ollama:')
            fix_timeout = 360 if is_local else 180
            fix_maxtok = 2500 if is_local else 8000
            fixed = call_model(
                spec,
                'Eres un fixer de código. Devuelve solo el código arreglado, sin markdown.',
                prompt, max_tokens=fix_maxtok, timeout=fix_timeout,
            )
            if not fixed or fixed.strip() == original.strip():
                log(f'  [fixer] {spec.id} no produjo cambio')
                continue

            # Quitar markdown si lo agregó.
            cleaned = re.sub(r'^```(?:typescript|ts|js)?\n', '', fixed)
            cleaned = re.sub(r'\n```$', '', cleaned)
            if not cleaned.strip() or cleaned.strip() == original.strip():
                continue

            # Dry-run: log sin aplicar.
            if args.dry_run:
                log(f'  [fixer] DRY-RUN {bug.suspect.file}:{bug.suspect.line} '
                    f'({spec.id}) — resultado:\n{cleaned[:500]}...')
                with STATS_LOCK:
                    STATS['fixes_succeeded'] += 1
                MEM.record_bug(bug.suspect.file, bug.suspect.line,
                               bug.suspect.snippet, fix_commit='DRY-RUN')
                continue

            # Aplicar y validar.
            if _apply_and_validate(path, cleaned, bug.suspect):
                commit = _commit_fix(path, bug.suspect)
                if commit:
                    with STATS_LOCK:
                        STATS['fixes_succeeded'] += 1
                    MEM.record_bug(bug.suspect.file, bug.suspect.line,
                                   bug.suspect.snippet, fix_commit=commit)
                    if _is_risky(bug.suspect):
                        with STATS_LOCK:
                            STATS['fixes_to_review'] += 1
                        _add_to_review(bug.suspect, commit, spec.id)
                    log(f'  [fixer] OK {bug.suspect.file}:{bug.suspect.line} '
                        f'({spec.id} → {commit[:7]})')
                else:
                    # Aplicado pero no comiteado (validó pero git falló).
                    # Limpiar working tree + index antes de propagar el error;
                    # si no, el próximo fixer leerá la versión parchada como
                    # `original` y aplicará un fix encima del fix anterior.
                    rel = path.relative_to(WT)
                    if _looks_like_patch(cleaned):
                        _revert_patch(cleaned)
                    try:
                        subprocess.run(
                            ['git', 'checkout', '--', str(rel)],
                            cwd=str(WT), capture_output=True, timeout=10,
                        )
                    except Exception as e:
                        log(f'  [fixer] no se pudo hacer git checkout tras fallo de commit: {e}')
                    log(f'  [fixer] aplicado pero sin commit: {path} (revertido)')
                update_thread_state(f'fixer-{bug.suspect.file}', status='done',
                                    current_task='OK')
                return
            else:
                # Validación falló: revertir y probar siguiente fixer.
                path.write_text(original)
                log(f'  [fixer] {spec.id} falló la validación, revertido')

        log(f'  [fixer] TODOS los fixers fallaron para {bug.suspect.file}:{bug.suspect.line}')
        update_thread_state(f'fixer-{bug.suspect.file}', status='done',
                            current_task='todos fallaron')
    finally:
        lock.release()


def _looks_like_patch(text: str) -> bool:
    return harness_gates.looks_like_patch(text)


def _apply_patch(diff_text: str) -> bool:
    return harness_gates.apply_patch(diff_text, wt=WT, log=log)


def _revert_patch(diff_text: str) -> bool:
    return harness_gates.revert_patch(diff_text, wt=WT, log=log)


def _patch_files(diff_text: str) -> set:
    return harness_gates.patch_files(diff_text)


def _apply_and_validate(path: Path, new_content: str, suspect: Suspect) -> bool:
    return harness_gates.apply_and_validate(
        path,
        new_content,
        suspect,
        subdir=SUBDIR,
        log=log,
        looks_like_patch_fn=_looks_like_patch,
        patch_files_fn=_patch_files,
        apply_patch_fn=_apply_patch,
        revert_patch_fn=_revert_patch,
        run_tsc_fn=_run_tsc,
        run_smoke_subset_fn=_run_smoke_subset,
    )


def _run_tsc() -> bool:
    return harness_gates.run_tsc(wt=WT, subdir=SUBDIR)


def _route_for_file(file_path: str) -> Optional[str]:
    return harness_gates.route_for_file(file_path)


def _dev_server_up() -> bool:
    return harness_gates.dev_server_up(DEV_URL)


def _run_e2e_route(route: str, max_actions: int = 12, budget_ms: int = 45000,
                   timeout: int = 90) -> tuple:
    return harness_gates.run_e2e_route(
        route,
        dev_url=DEV_URL,
        storage=STORAGE,
        wt_subdir=WT_SUBDIR,
        runner=Path(__file__).parent / 'e2e-runner.js',
        max_actions=max_actions,
        budget_ms=budget_ms,
        timeout=timeout,
        log=log,
    )


def _run_smoke_subset(file_path: str) -> bool:
    return harness_gates.run_smoke_subset(
        file_path,
        dev_url=DEV_URL,
        log=log,
        is_risky_path=tiers.is_risky_path,
        dev_server_up_fn=_dev_server_up,
        route_for_file_fn=_route_for_file,
        run_e2e_route_fn=_run_e2e_route,
    )


def _commit_fix(path: Path, suspect: Suspect) -> Optional[str]:
    return harness_gates.commit_fix(path, suspect, wt=WT, log=log)


def _is_risky(suspect: Suspect) -> bool:
    """True si el fix toca dinero, stock, estado, o auth."""
    full = (suspect.file + ' ' + suspect.why + ' ' + suspect.snippet).lower()
    return any(kw in full for kw in RISKY_KEYWORDS)


def _add_to_review(suspect: Suspect, commit: str, model: str) -> None:
    harness_review.add_to_review(
        suspect,
        commit,
        model,
        review_file=REVIEW_FILE,
        lock=REVIEW_LOCK,
    )


# ---------------------------------------------------------------------------
# E2E Sweep (Playwright en paralelo con el code hunter).
# ---------------------------------------------------------------------------

def _map_e2e_route_to_file(route: str) -> Optional[str]:
    return harness_gates.map_e2e_route_to_file(route, wt_subdir=WT_SUBDIR, log=log)

def _e2e_task_worker(wid: int = 0) -> None:
    """Worker E2E: consume tareas de `e2e_taskq` y emite hallazgos a suspectq.

    Cada task es (route, module_label, depth_hint). El worker:
      1. Construye el escenario JSON para e2e-runner.js.
      2. Ejecuta el crawler Playwright contra `route`.
      3. Parsea errores (consola/#419/HTTP≥400/UI).
      4. Mapea cada error a un archivo del worktree.
      5. Emite un Suspect a suspectq para que pase por filter→judge→fixer.

    Si la cola se vacía, espera bloqueado. Cuando llega un task, lo procesa.
    Si el dev server está caído, sale con un warning (el watchdog lo despertará).
    """
    runner = str(Path(__file__).parent / 'e2e-runner.js')
    log(f'  [e2e-{wid}] worker arrancando (consume e2e_taskq)')
    while not shutdown.is_set():
        try:
            task = e2e_taskq.get(timeout=5)
        except queue.Empty:
            continue
        route, module_label, depth_hint = task
        # El productor usa un sentinel None para shutdown ordenado.
        if route is None:
            e2e_taskq.task_done()
            break
        if not DEV_URL:
            e2e_taskq.task_done()
            continue
        # Si el dev server no responde, no malgastemos budget; el watchdog lo revive.
        if not _dev_server_alive():
            with STATS_LOCK:
                STATS['e2e_devserver_skips'] = STATS.get('e2e_devserver_skips', 0) + 1
            e2e_taskq.task_done()
            time.sleep(2)
            continue
        scn = {
            'baseURL': DEV_URL,
            'route': route,
            'maxActions': int(os.environ.get('LOOP_E2E_ACTIONS', '24')),
            'budgetMs': int(os.environ.get('LOOP_E2E_BUDGET_MS', '70000')),
        }
        if STORAGE:
            scn['storageState'] = STORAGE
        env = dict(os.environ)
        env.setdefault('DASHBOARD_NM', str(WT_SUBDIR / 'node_modules'))
        try:
            result = subprocess.run(
                ['node', runner], input=json.dumps(scn),
                cwd=str(WT_SUBDIR), env=env,
                capture_output=True, text=True, timeout=180,
            )
            out = json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            log(f'  [e2e-{wid}] {route} timeout')
            e2e_taskq.task_done()
            continue
        except json.JSONDecodeError:
            log(f'  [e2e-{wid}] {route} salida no-JSON: {(result.stdout or "")[:160]}')
            e2e_taskq.task_done()
            continue
        if out.get('fatal'):
            log(f'  [e2e-{wid}] {route} fatal: {str(out.get("fatal"))[:140]}')
            e2e_taskq.task_done()
            continue
        errors = out.get('errors', [])
        if errors:
            log(f'  [e2e-{wid}] {route} → {len(errors)} hallazgos (mod={module_label})')
        for err in errors:
            with STATS_LOCK:
                STATS['e2e_findings'] += 1
            kind = err.get('kind', '?')
            where = err.get('where', route)
            detail = str(err.get('detail', ''))
            e2e_route = out.get('route', route)
            mapped_file = _map_e2e_route_to_file(e2e_route)
            if not mapped_file:
                log(f'    [e2e-{wid}] route {e2e_route} no mapeada a archivo; skip')
                continue
            # Emitir un Suspect al pipeline filter→judge→fixer.
            sus = Suspect(
                file=mapped_file,
                line=int(err.get('line', 1)) if str(err.get('line', '')).isdigit() else 1,
                snippet=str(err.get('detail', ''))[:200],
                why=f'E2E {kind} en {route} ({module_label}): {detail[:160]}',
                severity='high' if kind in ('http500', 'crash', 'fatal') else 'medium',
                repro=f'curl {DEV_URL}{route}  (ver consola + network tab)',
                confidence=0.7,
                samples=1,
                source='e2e',
            )
            yield_suspect(sus)
        e2e_taskq.task_done()
    log(f'  [e2e-{wid}] worker terminado')


def _dev_server_alive() -> bool:
    return harness_gates.dev_server_alive(DEV_URL)


def e2e_producer(stop_event: threading.Event) -> None:
    """Productor: llena e2e_taskq con rutas del orquestador + zonas + journeys.

    Estrategia "abundancia":
      - Rutas de discover_zones (panel modules) — una vez cada una.
      - Rutas de las journeys del orquestador (primer step de cada journey).
      - Sub-rutas de las journeys (steps siguientes) para multi-paso.
      - Vuelve a encolar si la cola baja de un umbral (siempre tiene tareas).
    """
    log('  [e2e-prod] productor arrancando')
    produced = set()
    while not stop_event.is_set():
        # 1) Rutas base del panel.
        with _ORCH_LOCK:
            orch_journeys = list(_current_e2e_journeys)
        base_routes: list[tuple[str, str, int]] = []
        for zone in discover_zones():
            route = '/' + zone.replace('app/(panel)/', '').strip('/')
            base_routes.append((route, zone.split('/')[-1] or 'panel', 1))
        # 2) Rutas de las journeys (primer step y resto de steps).
        for j in orch_journeys:
            mod = j.module or 'general'
            for i, step in enumerate(j.steps or []):
                # step puede ser una URL o una descripción. Si parece URL, usar.
                if isinstance(step, str) and step.startswith('/'):
                    base_routes.append((step, mod, min(i + 1, 3)))
                # Si no, generar un slug de búsqueda en zonas del módulo.
                else:
                    slug = (step or '').lower().replace(' ', '-')[:40]
                    for zone in discover_zones():
                        if mod in zone or slug in zone.lower():
                            route = '/' + zone.replace('app/(panel)/', '').strip('/')
                            base_routes.append((route, mod, min(i + 1, 3)))
        # 3) Rutas de auth/públicas (siempre vale la pena crawlearlas).
        for extra in ['/sign-in', '/sign-up', '/inicio', '/']:
            base_routes.append((extra, 'auth', 1))
        # Encolar las nuevas.
        added = 0
        for route, mod, depth in base_routes:
            key = (route, mod, depth)
            if key in produced:
                continue
            produced.add(key)
            try:
                e2e_taskq.put_nowait((route, mod, depth))
                added += 1
            except queue.Full:
                break
        if added:
            log(f'  [e2e-prod] encoladas {added} tareas nuevas (queue={e2e_taskq.qsize()})')
        # Esperar a que baje el backlog antes de re-producir (mantiene cola larga).
        for _ in range(60):
            if stop_event.is_set():
                break
            if e2e_taskq.qsize() < 50:
                break
            time.sleep(2)
    log('  [e2e-prod] productor terminado')


# ---------------------------------------------------------------------------
# Watchdog.
# ---------------------------------------------------------------------------

shutdown = threading.Event()


def watchdog() -> None:
    """Si no hay avance en N segundos, libera modelos pesados (Ollama unload)
    y avisa al usuario."""
    last_progress = time.time()
    with STATS_LOCK:
        last_round = STATS['rounds']
    while not shutdown.is_set():
        time.sleep(30)
        with STATS_LOCK:
            current_round = STATS['rounds']
            fixes_succeeded = STATS['fixes_succeeded']
        if current_round > last_round or fixes_succeeded > 0:
            last_progress = time.time()
            last_round = current_round
        elif time.time() - last_progress > STALL_SECS:
            log(f'  [watchdog] stall {STALL_SECS}s sin avance; descargando Ollama')
            try:
                # Forzar unload de los modelos del fleet (por nodo) para liberar VRAM.
                default_base = os.environ.get('LOOP_OLLAMA_BASE') or 'http://host.docker.internal:11434'
                if FLEET is not None:
                    unload = [(getattr(s, 'base_url', None) or default_base, s.id.split(':', 1)[1])
                              for s in FLEET.all_specs()]
                else:
                    # Fallback: usar modelos de TIER0_HUNTERS actuales
                    model_names = list({
                        spec.id.split(':', 1)[1]
                        for spec in tiers.TIER0_HUNTERS
                        if spec.id.startswith('ollama:')
                    })
                    if not model_names:
                        model_names = ['qwen3-coder:30b']
                    unload = [(default_base, m) for m in model_names]
                for base, model_name in unload:
                    try:
                        req = urllib.request.Request(
                            f'{base}/api/generate',
                            data=json.dumps({'model': model_name, 'keep_alive': 0}).encode(),
                            headers={'Content-Type': 'application/json'},
                        )
                        urllib.request.urlopen(req, timeout=5)
                    except Exception:
                        pass
                log('  [watchdog] Ollama unload enviado')
            except Exception:
                pass
            last_progress = time.time()
        write_status()


# ---------------------------------------------------------------------------
# Main loop: discovery de zonas, arranque de workers, converge-and-stop.
# ---------------------------------------------------------------------------

# Cargar memoria.
MEM = memory.Memory.load()


def discover_zones() -> list[str]:
    return harness_discovery.discover_zones(wt=WT, subdir=SUBDIR)


def discover_code_zones() -> list[list[str]]:
    return harness_discovery.discover_code_zones(wt=WT, subdir=SUBDIR)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-e2e', action='store_true', help='skip E2E sweep')
    parser.add_argument('--dry-run', action='store_true', help='no aplicar fixes')
    parser.parse_args(namespace=args)

    # LOOP_OLLAMA_GPU_CONC=0 → el semáforo de _ollama_sem tiene 0 permits y
    # TODAS las llamadas a Ollama se cuelgan en silencio. Validar ANTES de
    # arrancar cualquier worker.
    try:
        _ollama_conc = int(os.environ.get('LOOP_OLLAMA_GPU_CONC', '1'))
    except ValueError:
        log(f'  ERROR: LOOP_OLLAMA_GPU_CONC debe ser un entero, '
            f'got {os.environ.get("LOOP_OLLAMA_GPU_CONC")!r}')
        sys.exit(1)
    if _ollama_conc <= 0:
        log(f'  ERROR: LOOP_OLLAMA_GPU_CONC debe ser > 0, got {_ollama_conc}')
        sys.exit(1)

    log('=' * 78)
    log('  HARNESS v7 — Adaptativo, Cuota-Aware, Memoria Persistente')
    log('=' * 78)
    log(f'  Tier 0 hunters:        {[s.id for s in tiers.get_tier0_hunters()]}')
    log(f'  Tier 0 second opinion: {[s.id for s in tiers.get_tier0_30b()]}')
    log(f'  Tier 1 filters:        {[s.id for s in tiers.get_tier1_filters()]}')
    log(f'  Tier 2 judges:         {[s.id for s in tiers.get_tier2_judges()]}')
    log(f'  Tier 3 fixers:         {[s.id for s in tiers.get_tier3_fixers()]}')
    log(f'  Tier 4 arbiters:       {[s.id for s in tiers.get_tier4_arbiters()]}')
    log('')
    log(f'  Worktree:              {WT}')
    log(f'  Subdir:                {SUBDIR}')
    log(f'  Dev URL:               {DEV_URL}')
    log(f'  Target clean rounds:   {TARGET}')
    log(f'  Hours budget:          {HOURS}')
    log('')
    log('  Pre-warming quota cache (no bloqueante, 30s timeout)...')
    # El primer refresh puede tardar 30s+. Lo hacemos en un thread daemon
    # para no bloquear el arranque. Si no termina a los 30s, seguimos
    # con el cache vacío (todos los modelos cloud se tratarán como OK,
    # que es el comportamiento conservador inicial).
    quota_ready = threading.Event()
    def _warm_quota():
        try:
            quota.refresh(force=True)
        except Exception as e:
            log(f'  [quota] warm falló: {e}')
        finally:
            quota_ready.set()
    threading.Thread(target=_warm_quota, daemon=True, name='quota-warm').start()
    quota_ready.wait(timeout=30)
    if quota_ready.is_set():
        log(quota.report())
    else:
        log('  [quota] warm no terminó en 30s; arrancando con cache vacío (todos los modelos pasan)')
    log(MEM.summary())
    log('')

    # Cuarentena inicial de modelos zombi conocidos (gratis, inmediata).
    seed_quarantine()

    # Cargar tiers filtrados por cuota.
    # Fleet: descubre GPUs/nodos y planifica el modelo por VRAM (Fase B/C). Los
    # hunters locales y el fixer local salen del fleet (incl. la RTX 2060 antes
    # ociosa). Health-loop en daemon escribe /tmp/harness-fleet.json para la TUI.
    global FLEET, _current_orch_prompts, _current_e2e_journeys
    FLEET = fleet.build_fleet()
    fleet.write_fleet_status(FLEET)
    fleet.start_health_loop(FLEET, shutdown)
    log(f'  Fleet: {[(getattr(s, "node", None), getattr(s, "gpu_index", None), s.id) for s in FLEET.all_specs()]}')

    hunter_specs = FLEET.hunter_specs() or quota.filter_tier(tiers.get_tier0_hunters(), 'tier0') or list(tiers.TIER0_HUNTERS)
    second_specs = FLEET.reasoner_specs() or quota.filter_tier(tiers.get_tier0_30b(), 'tier0') or list(tiers.TIER0_30B_OPINIONS)
    cloud_hunter_specs = quota.filter_tier(tiers.get_tier0_cloud_hunters(), 'tier0') or list(tiers.TIER0_CLOUD_HUNTERS)
    filter_specs = quota.filter_tier(tiers.get_tier1_filters(), 'tier1') or list(tiers.TIER1_FILTERS)
    judge_specs = quota.filter_tier(tiers.get_tier2_judges(), 'tier2')[:JUDGE_TOTAL] or list(tiers.TIER2_JUDGES[:JUDGE_TOTAL])

    # Aplicar configuración de modelos desde la TUI (tecla M).
    # El archivo /tmp/harness-models.json contiene qué modelos habilitó el usuario
    # para cada tier. Si existe, filtra los tiers a SOLO esos modelos.
    mcfg_path = '/tmp/harness-models.json'
    if os.path.exists(mcfg_path):
        try:
            with open(mcfg_path) as f:
                mcfg = json.load(f)
            mcfg_t1 = set(mcfg.get('tier1_filters', []))
            mcfg_t2 = set(mcfg.get('tier2_judges', []))
            mcfg_t3 = set(mcfg.get('tier3_fixers', []))
            mcfg_t4 = set(mcfg.get('tier4_arbiters', []))
            if mcfg_t1:
                filter_specs = [s for s in filter_specs if s.id in mcfg_t1]
                if not filter_specs:
                    filter_specs = [s for s in tiers.TIER1_FILTERS if s.id in mcfg_t1]
            if mcfg_t2:
                judge_specs = [s for s in judge_specs if s.id in mcfg_t2]
                if not judge_specs:
                    judge_specs = [s for s in tiers.TIER2_JUDGES if s.id in mcfg_t2]
            # tier3_fixers y tier4_arbiters NO se filtran todavía: el fixer chain
            # (en fixer_worker) y el tier 4 (árbitros) aún no leen mcfg_t3/mcfg_t4.
            # Las variables quedan disponibles para `any_filtered` y para uso futuro.
            any_filtered = mcfg_t1 or mcfg_t2 or mcfg_t3 or mcfg_t4
            if any_filtered:
                log('  [main] Config de modelos aplicada desde TUI (/tmp/harness-models.json)')
        except (json.JSONDecodeError, OSError):
            pass
    log(f'  Hunter specs activos:  {[s.id for s in hunter_specs]}')
    log(f'  Second specs activos:  {[s.id for s in second_specs]}')
    log(f'  Cloud hunter specs:    {[s.id for s in cloud_hunter_specs]}')
    log(f'  Filter specs activos:  {[s.id for s in filter_specs]}')
    log(f'  Judge specs activos:   {[s.id for s in judge_specs]}')
    log(f'  Cuarentena inicial:    {quarantined_models()}')
    log('')

    # Probe de salud ACTIVO opcional (LOOP_HEALTH_PROBE=1): pre-cuarentena los
    # zombis desconocidos antes de que atasquen las colas. Off por defecto
    # (cuesta algo de cuota; la marca perezosa en call_model cubre el resto).
    if os.environ.get('LOOP_HEALTH_PROBE', '0') == '1':
        log('  [health] probe activo de arranque...')
        health_probe_all(list(hunter_specs) + list(cloud_hunter_specs)
                         + list(filter_specs) + list(judge_specs))
        log(f'  [health] tras probe, en cuarentena: {quarantined_models()}')

    # Discovery.
    code_zones = discover_code_zones()
    if not code_zones:
        log('  ✖ no se encontraron zonas de código. ¿Worktree correcto?')
        return
    log(f'  Zonas de código: {len(code_zones)} (suma {sum(len(z) for z in code_zones)} archivos)')

    # Orquestador de prompts: genera prompts DIRIGIDOS con conocimiento del dominio.
    # Usa un modelo de alto contexto (gemini-2.5-flash-lite, 1M ctx) para entender
    # el proyecto y guiar a los hunters hacia bugs reales, no ruido.
    log('')
    log('  [main] Generando prompts de caza con orquestador...')
    hunting_prompts = orchestrator.generate_hunting_prompts(
        str(WT), SUBDIR, cache_path='/tmp/orch-prompts.json'
    )
    with _ORCH_LOCK:
        _current_orch_prompts = hunting_prompts or []
    if hunting_prompts:
        log(f'  [main] Orquestador: {len(hunting_prompts)} prompts dirigidos generados')
        # Log de muestra: primeros 3 prompts
        for i, p in enumerate(hunting_prompts[:3]):
            reason = p.reason if hasattr(p, 'reason') else p.get('reason', '?')
            file = p.file if hasattr(p, 'file') else p.get('file', '?')
            log(f'         [{i+1}] {file}: {reason[:100]}')
        # Generar también journeys E2E si no se desactivaron.
        if not args.no_e2e:
            e2e_journeys = orchestrator.generate_e2e_journeys(
                str(WT), SUBDIR, cache_path='/tmp/orch-prompts.json'
            )
            if e2e_journeys:
                with _ORCH_LOCK:
                    _current_e2e_journeys = e2e_journeys
                log(f'  [main] Orquestador: {len(e2e_journeys)} journeys E2E generados')
    else:
        log('  [main] Orquestador no generó prompts (cache o fallback heurístico)')
        hunting_prompts = None
    log('')

    # Watchdog.
    threading.Thread(target=watchdog, daemon=True, name='watchdog').start()

    # Thread registry para shutdown ordenado
    worker_threads = []

    # Arranque de workers tier 0.
    total_hunter_workers = HUNTER_HILOS + SECOND_HILOS + CLOUD_HUNTER_HILOS
    zone_for_wid = {wid: [] for wid in range(total_hunter_workers)}
    worker_cycle = itertools.cycle(range(total_hunter_workers))
    for zone in code_zones:
        zone_for_wid[next(worker_cycle)].extend(zone)
    if len(code_zones) < total_hunter_workers:
        zone_cycle = itertools.cycle(code_zones)
        for wid in range(total_hunter_workers):
            if not zone_for_wid[wid]:
                zone_for_wid[wid].extend(next(zone_cycle))
    hunter_threads = []
    for i in range(HUNTER_HILOS):
        t = threading.Thread(
            target=hunter_worker,
            args=(zone_for_wid[i], hunter_specs[i % len(hunter_specs)], i),
            kwargs={'hunting_prompts': hunting_prompts},
            daemon=True,
            name=f'hunter-{i}',
        )
        t.start()
        hunter_threads.append(t)
        worker_threads.append(t)

    second_threads = []
    for i in range(SECOND_HILOS):
        global_wid = HUNTER_HILOS + i
        t = threading.Thread(
            target=hunter_worker,
            args=(zone_for_wid[global_wid], second_specs[i % len(second_specs)], i),
            kwargs={'hunting_prompts': hunting_prompts},
            daemon=True,
            name=f'second-{i}',
        )
        t.start()
        second_threads.append(t)
        worker_threads.append(t)

    # Arranque de workers tier 0 CLOUD (modelos chiquitos baratos).
    for i in range(CLOUD_HUNTER_HILOS):
        global_wid = HUNTER_HILOS + SECOND_HILOS + i
        t = threading.Thread(
            target=hunter_worker,
            args=(zone_for_wid[global_wid],
                  cloud_hunter_specs[i % len(cloud_hunter_specs)], i),
            kwargs={'hunting_prompts': hunting_prompts},
            daemon=True,
            name=f'cloud-{i}',
        )
        t.start()
        worker_threads.append(t)

    # Arranque de workers tier 1 (filtros).
    for i in range(FILTER_HILOS):
        t = threading.Thread(
            target=filter_worker,
            args=(filter_specs, i),
            daemon=True,
            name=f'filter-{i}',
        )
        t.start()
        worker_threads.append(t)

    # Arranque de workers tier 2 (jueces).
    for i in range(JUDGE_HILOS):
        t = threading.Thread(
            target=judge_worker,
            args=(judge_specs, i),
            daemon=True,
            name=f'judge-{i}',
        )
        t.start()
        worker_threads.append(t)

    # E2E: productor + N workers que consumen e2e_taskq (cola larga).
    # El productor llena con discover_zones + journeys del orquestador +
    # rutas de auth. Los workers crawlean en paralelo y emiten a suspectq.
    E2E_WORKERS = int(os.environ.get('LOOP_E2E_WORKERS', '8'))
    if not args.no_e2e:
        # 1) Productor: mantiene la cola llena.
        t = threading.Thread(
            target=e2e_producer, args=(shutdown,), daemon=True, name='e2e-prod'
        )
        t.start()
        worker_threads.append(t)
        # 2) Workers consumidores: N hilos que sacan tareas de e2e_taskq.
        for i in range(E2E_WORKERS):
            t = threading.Thread(
                target=_e2e_task_worker, args=(i,), daemon=True, name=f'e2e-w{i}'
            )
            t.start()
            worker_threads.append(t)
        log(f'  [main] E2E: productor + {E2E_WORKERS} workers activos')

    # Main loop: converge-and-stop. NO termina el run prematuramente;
    # solo avisa cuando hay N rondas limpias (el código está estable). El run
    # termina por `deadline` (HOURS), no por converge.
    deadline = time.time() + HOURS * 3600
    last_status = 0
    try:
        while time.time() < deadline:
            time.sleep(15)
            with STATS_LOCK:
                STATS['rounds'] += 1
                if STATS['fixes_succeeded'] > 0 or STATS['e2e_findings'] > 0:
                    STATS['clean_rounds_in_a_row'] = 0
                else:
                    STATS['clean_rounds_in_a_row'] += 1
                current_rounds = STATS['rounds']
                clean_rounds = STATS['clean_rounds_in_a_row']
                fixes_succeeded = STATS['fixes_succeeded']

            if time.time() - last_status > 60:
                last_status = time.time()
                log(f'  [main] round={current_rounds} clean={clean_rounds} '
                    f'suspectq={suspectq.qsize()} bugq={bugq.qsize()} '
                    f'fixes={fixes_succeeded}')
                write_status()

            if clean_rounds >= TARGET:
                # NO break — solo avisamos. El run sigue hasta `deadline`.
                log(f'  [main] converge-adv: {clean_rounds} rondas '
                    f'limpias (target={TARGET}), código estable. Continuando '
                    f'hasta deadline ({int((deadline-time.time())/60)}m restantes).')
    except KeyboardInterrupt:
        log('  [main] KeyboardInterrupt, parando...')

    shutdown.set()
    write_status()
    log(f'  [main] shutdown @ round {STATS["rounds"]}')
    log('  [main] aguardando workers...')
    for t in worker_threads:
        t.join(timeout=5.0)
    with STATS_LOCK:
        final_round = STATS['rounds']
    log(f'  [main] {len([t for t in worker_threads if t.is_alive()])} workers aún corriendo')
    MEM.save()
    log('  [main] Memorias guardadas, run terminado')

    log(quota.report())
    log(MEM.summary())


if __name__ == '__main__':
    main()
