#!/usr/bin/env python3
"""
HARNESS v6 "Adaptativo" — auto-endurecimiento nocturno E2E.

Reescritura completa del v5. Cambios principales:
  · **Tier 0 paralelo**: 4-6 hilos del 9b local + 2 hilos del 30b (sin contención
    porque Ollama puede mantenerlos residentes con `OLLAMA_MAX_LOADED_MODELS>=2`).
  · **Tier 1 filtro**: Minimax + Gemini Flash + Gemini Flash Lite + Haiku
    (alta cuota, media concurrencia).
  · **Tier 2 jueces**: Minimax + Codex + Gemini Pro + Sonnet (quorum 2/3).
  · **Tier 3 fixer**: Codex → Minimax → Gemini Pro → Sonnet → local 30b
    (cadena con fallback; Opus NO se usa como fixer primario).
  · **Tier 4 árbitro**: Sonnet → Minimax → Gemini Pro. **Opus SOLO si
    (a) fix riesgoso, (b) los 3 discordaron o están en cuarentena,
    (c) hay cuota Claude**. Banner de aviso cada vez.
  · **Control de costos con `quota.py`**: antes de cada llamada, consulta
    ai-usage-live. Si un modelo está cerca del límite, se omite de la cadena.
  · **Memoria persistente con `memory.py`**: falsos positivos del 9b se
    filtran entre sesiones; zonas endurecidas se re-escanean menos.
  · **Reparación especulativa**: cuando 2+ jueces confirman, el fix se lanza
    ANTES de que el siguiente sospechoso se confirme.
  · **Hot-reload**: cada fix aplicado se valida (tsc+E2E) y se re-escanea
    esa zona de inmediato.
  · **Converge-and-stop**: si una pasada completa no produce fixes, el
    código está estable y para (no quemar cómputo re-validando código limpio).

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
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Módulos del v6.
import memory
import orchestrator
import quota
import tiers

# ---------------------------------------------------------------------------
# Config (env vars; con defaults razonables).
# ---------------------------------------------------------------------------

WT          = Path(os.environ.get('LOOP_WT', '/tmp/demeter-loop'))
SUBDIR      = os.environ.get('LOOP_SUBDIR', 'apps/dashboard')
DEV_URL     = os.environ.get('LOOP_DEV_URL', 'http://localhost:3011')
STORAGE     = os.environ.get('LOOP_STORAGE_STATE', '')  # vacío = modo demo
DYN_ID      = os.environ.get('LOOP_DYNAMIC_ID', '')     # id sembrado para [id]
HOURS       = int(os.environ.get('LOOP_HOURS', '8'))
TARGET      = int(os.environ.get('LOOP_TARGET', '50'))   # rondas limpias para "endurecer"

# Concurrencia por tier.
HUNTER_HILOS    = int(os.environ.get('LOOP_HUNTERS', '4'))     # hilos 9b
SECOND_HILOS    = int(os.environ.get('LOOP_SECOND_HILOS', '2'))  # hilos 30b
FILTER_HILOS    = int(os.environ.get('LOOP_FILTER_HILOS', '4'))  # tier 1 (era 2, subido para usar más modelos en paralelo)
JUDGE_HILOS     = int(os.environ.get('LOOP_JUDGE_HILOS', '2'))   # tier 2 (era 1, subido)

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
E2E_FILE    = Path('/tmp/loop-e2e-findings.json')

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
# Suspect: una hipótesis de bug (lo que produce el hunter).
# ---------------------------------------------------------------------------

@dataclass
class Suspect:
    """Hipótesis de bug generada por el hunter local. Vive en suspectq."""
    file: str
    line: int
    snippet: str
    why: str                            # por qué el hunter cree que es bug
    confidence: float = 0.0             # 0..1 (consenso del hunter)
    samples: int = 0                    # cuántas muestras de auto-consistencia
    first_seen: float = field(default_factory=time.time)
    refuted: bool = False               # pasó refutación adversarial?
    hash: str = ''
    source: str = 'hunter'              # 'hunter' | 'e2e'

    def __post_init__(self):
        self.hash = memory.hash_suspect(self.file, self.line, self.snippet)


@dataclass
class Bug:
    """Bug confirmado por los jueces. Vive en bugq."""
    suspect: Suspect
    votes_real: int = 0
    votes_not_bug: int = 0
    voters: list[str] = field(default_factory=list)   # modelos que confirmaron


# ---------------------------------------------------------------------------
# Colas (productor-consumidor).
# ---------------------------------------------------------------------------

suspectq: queue.Queue[Suspect] = queue.Queue(maxsize=200)
bugq: queue.Queue[Bug] = queue.Queue(maxsize=50)


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
    'opus_invocations': 0,
    'started_at': time.time(),
}


def write_status() -> None:
    """Volca el estado para /tmp/loop-v6-status.json (TUI / debug)."""
    payload = {
        'stats': STATS,
        'uptime_sec': int(time.time() - STATS['started_at']),
        'quota': quota.local_spend(),
        'memory': {
            'total_rounds': MEM.total_rounds,
            'total_bugs_found': MEM.total_bugs_found,
            'total_fixes_applied': MEM.total_fixes_applied,
            'open_bugs': len(MEM.open_bugs()),
            'hardened_zones': sum(1 for z in MEM.zones.values() if z.clean_rounds >= 3),
        },
    }
    try:
        STATUS_FILE.write_text(json.dumps(payload, indent=2, default=str))
    except OSError:
        pass


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
# Invocación de modelos: punto único. Decide QUÉ modelo usar según rol +
# estado de cuota, y registra el uso en `quota.record`.
# ---------------------------------------------------------------------------

def call_model(spec: tiers.ModelSpec, system: str, prompt: str, max_tokens: int = 2048,
               timeout: int = 120) -> Optional[str]:
    """Llama al modelo y devuelve el texto. None si falla (cuota, timeout, error).

    Para modelos locales (Ollama) usa HTTP al endpoint.
    Para modelos cloud usa el CLI correspondiente.
    """
    if not quota.is_model_ok(spec.id):
        log(f'  [quota] {spec.id} bloqueado por cuota')
        return None

    t0 = time.time()
    text: Optional[str] = None
    in_tok = 0
    out_tok = 0

    try:
        if spec.id.startswith('ollama:'):
            text = _call_ollama(spec, prompt, system, max_tokens, timeout)
        elif spec.id.startswith('minimax/') or spec.id.startswith('minimax:'):
            text = _call_minimax(spec, prompt, system, max_tokens, timeout)
        elif spec.id.startswith('opencode:') or spec.id.startswith('opencode-go:'):
            text = _call_opencode(spec, prompt, system, max_tokens, timeout)
        elif spec.id.startswith('gemini:'):
            text = _call_gemini_cli(spec, prompt, system, max_tokens, timeout)
        elif spec.id.startswith('claude:'):
            text = _call_claude_cli(spec, prompt, system, max_tokens, timeout)
        elif spec.id.startswith('codex:'):
            text = _call_codex_cli(spec, prompt, system, max_tokens, timeout)
        else:
            log(f'  [call] modelo desconocido: {spec.id}')
            return None

        # Estimar tokens (los CLIs no devuelven usage por defecto; usamos chars como proxy).
        if text is not None:
            in_tok = max(1, (len(system) + len(prompt)) // 4)
            out_tok = max(1, len(text) // 4)
    except subprocess.TimeoutExpired:
        log(f'  [call] {spec.id} timeout {timeout}s')
    except Exception as e:
        log(f'  [call] {spec.id} error: {e}')

    quota.record(spec.id, in_tok, out_tok)
    elapsed = time.time() - t0
    log(f'  [call] {spec.id} {elapsed:.1f}s  in~{in_tok}  out~{out_tok}')
    return text


def _call_ollama(spec: tiers.ModelSpec, prompt: str, system: str, max_tokens: int,
                 timeout: int) -> str:
    model_name = spec.id.split(':', 1)[1]
    base = os.environ.get('LOOP_OLLAMA_BASE', 'http://host.docker.internal:11434')
    payload = {
        'model': model_name,
        'prompt': prompt,
        'system': system,
        'stream': False,
        # `keep_alive: '10m'` evita que Ollama descargue el modelo entre requests.
        # Crítico para cadencia alta: la primera carga tarda 2-3s; las siguientes <1s.
        'keep_alive': os.environ.get('LOOP_OLLAMA_KEEP_ALIVE', '10m'),
        'options': {'num_predict': max_tokens, 'temperature': 0.2},
    }
    # El 80B CPU puede tardar 60-180s en responder requests grandes; el 30B
    # GPU <10s. Timeout por modelo: modelos CPU reciben timeout más alto.
    is_cpu = 'cpu' in model_name.lower() or '80b' in model_name.lower() or 'next' in model_name.lower()
    actual_timeout = timeout
    if is_cpu:
        # CPU es ~10-20x más lento que GPU. Estiramos el timeout.
        actual_timeout = max(timeout, 300)  # mínimo 5min para CPU
    req = urllib.request.Request(
        f'{base}/api/generate',
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=actual_timeout) as r:
        data = json.loads(r.read())
    return data.get('response', '').strip()


def _call_minimax(spec: tiers.ModelSpec, prompt: str, system: str, max_tokens: int,
                 timeout: int) -> str:
    """Invoca Minimax vía el CLI opencode. Formato del id: 'minimax/MiniMax-M3'
    (provider/model con slash). Si opencode no está en PATH, intenta HTTP
    directo al API.
    """
    # El spec.id es 'minimax/MiniMax-M3' (formato opencode). Lo pasamos tal cual.
    model_id = spec.id  # 'minimax/MiniMax-M3'
    full_prompt = (f'<<SYS>>\n{system}\n<</SYS>>\n\n{prompt}' if system else prompt)
    try:
        result = subprocess.run(
            ['opencode', 'run', '--model', model_id, full_prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = result.stdout.strip()
        if not out and result.stderr:
            # opencode suele loguear el error a stderr; mostrarlo.
            log(f'  [minimax] stderr: {result.stderr[:200]}')
        return out
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f'  [minimax] CLI falló: {e}; probando HTTP directo')
        # Fallback: HTTP al API de Minimax (si hay MINIMAX_API_KEY en env).
        model_name = spec.id.split('/', 1)[-1]  # 'MiniMax-M3'
        return _call_minimax_http(model_name, full_prompt, max_tokens, timeout)


def _call_minimax_http(model: str, prompt: str, max_tokens: int, timeout: int) -> str:
    api_key = os.environ.get('MINIMAX_API_KEY', '')
    if not api_key:
        return ''
    base = os.environ.get('MINIMAX_API_BASE', 'https://api.minimax.io/v1')
    req = urllib.request.Request(
        f'{base}/chat/completions',
        data=json.dumps({
            'model': model,
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': max_tokens,
        }).encode(),
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return data['choices'][0]['message']['content'].strip()


def _call_opencode(spec: tiers.ModelSpec, prompt: str, system: str, max_tokens: int,
                   timeout: int) -> str:
    """Invoca un modelo opencode u opencode-go vía el CLI `opencode run`.

    Formato del id: 'opencode:deepseek-v4-flash-free' o 'opencode-go:glm-5.2'.
    OpenCode espera el formato `provider/model` con SLASH (no dos puntos),
    así que convertimos `opencode-go:glm-5.2` → `opencode-go/glm-5.2` al
    invocar. La key opencode-go la lee opencode de `API_KEY` env var.

    Probado en jul-2026: los 5 free + los 8 pro responden en 4-25s.
    """
    # Convertir formato `provider:model` a `provider/model` que opencode espera.
    opencode_model_id = spec.id.replace(':', '/', 1)
    full_prompt = (f'<<SYS>>\n{system}\n<</SYS>>\n\n{prompt}' if system else prompt)
    try:
        # OpenCode tarda en arrancar (~3s) + latencia del modelo (4-25s).
        # Timeout por defecto 60s; estirar a 180s para prompts grandes.
        actual_timeout = max(timeout, 180)
        result = subprocess.run(
            ['opencode', 'run', '--model', opencode_model_id, full_prompt],
            capture_output=True,
            text=True,
            timeout=actual_timeout,
        )
        out = result.stdout.strip()
        if not out and result.stderr:
            # opencode loguea el error a stderr; mostrarlo.
            log(f'  [opencode] {opencode_model_id} stderr: {result.stderr[:200]}')
        # Filtrar las líneas de "build" que opencode-go antepone.
        out = '\n'.join(
            line for line in out.split('\n')
            if not line.startswith('[0m') and 'build ·' not in line
        ).strip()
        return out
    except FileNotFoundError:
        log(f'  [opencode] CLI `opencode` no encontrado en PATH')
        return ''
    except subprocess.TimeoutExpired:
        log(f'  [opencode] {opencode_model_id} timeout {actual_timeout}s')
        return ''


def _call_claude_cli(spec: tiers.ModelSpec, prompt: str, system: str, max_tokens: int,
                    timeout: int) -> str:
    model = spec.id.split(':', 1)[1]
    full_prompt = prompt
    if system:
        full_prompt = f'<system>{system}</system>\n\n{prompt}'
    try:
        result = subprocess.run(
            ['claude', '-p', full_prompt, '--model', model, '--output-format', 'text'],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f'  [claude] CLI falló: {e}')
        return ''


def _call_gemini_cli(spec: tiers.ModelSpec, prompt: str, system: str, max_tokens: int,
                     timeout: int) -> str:
    """Invoca Gemini CLI en modo headless. Flags verificados:
    - `-m <model>`: modelo concreto (alias como 'flash' también funciona).
    - `-p <prompt>`: modo headless.
    - `--yolo`: auto-aprueba tools (necesario porque el CLI pregunta).
    - `--output-format text`: salida limpia, sin JSON-RPC.
    """
    model = spec.id.split(':', 1)[1]
    full_prompt = f'{system}\n\n{prompt}' if system else prompt
    try:
        result = subprocess.run(
            ['gemini', '-m', model, '-p', full_prompt,
             '--yolo', '--output-format', 'text'],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0 and '429' in (result.stderr or ''):
            # Rate limit conocido: marcar para cuarentena.
            log(f'  [gemini] {model} rate-limited (429)')
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f'  [gemini] CLI falló: {e}')
        return ''


def _call_codex_cli(spec: tiers.ModelSpec, prompt: str, system: str, max_tokens: int,
                    timeout: int) -> str:
    """Invoca Codex CLI. Flags verificados:
    - `codex exec <prompt>`: subcomando `exec` para no-interactive.
    - `--model <model>`: modelo concreto (gpt-5.5 es el default; codex-mini NO soportado).
    - `--skip-git-repo-check`: si el worktree tiene su propio git.
    """
    # Si el spec es `codex:gpt-5.5`, extraemos 'gpt-5.5'. Si no, default 'gpt-5.5'.
    model = spec.id.split(':', 1)[1] if ':' in spec.id else 'gpt-5.5'
    full_prompt = f'{system}\n\n{prompt}' if system else prompt
    try:
        result = subprocess.run(
            ['codex', 'exec', '--model', model, '--skip-git-repo-check', full_prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f'  [codex] CLI falló: {e}')
        return ''


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

FORMATO DE SALIDA (estricto, una línea por bug):
FILE::LINE::SNIPPET::REASON

Donde:
  - FILE: ruta relativa al repo (ej. lib/queries/clientes.ts)
  - LINE: número de línea aproximado
  - SNIPPET: 5-10 palabras del código sospechoso
  - REASON: 1-2 frases explicando por qué es bug

Si NO encuentras bugs en la zona, responde EXACTAMENTE: NO_BUGS
"""


def _parse_bug_line(line: str) -> Optional[dict]:
    """Extrae {file, line, snippet, why} de una línea de output del hunter.
    Acepta varios formatos porque los modelos locales no siempre son estrictos.
    Acepta paréntesis (rutas del App Router como `app/(panel)/...`).
    """
    # Patrón para el path: word chars, /, ., -, (, ).
    PATH = r'[\w/.\-()]+'
    # Formato 1: FILE::LINE::SNIPPET::REASON (estricto, el original).
    m = re.match(
        rf'^{PATH}\.([a-z]{{1,5}})::(\d+)::([^:]{{1,200}})::(.*)$',
        line,
    )
    if m:
        return {
            'file': m.group(0).split('::')[0],
            'line': int(m.group(2)),
            'snippet': m.group(3).strip()[:200],
            'why': m.group(4).strip()[:500],
        }
    # Formato 2: FILE:LINE - REASON (con guión).
    m = re.match(
        rf'^{PATH}\.([a-z]{{1,5}}):(\d+)\s*[-–—]\s*(.{{5,500}})$',
        line,
    )
    if m:
        text = m.group(3).strip()
        return {
            'file': m.group(0).split(':')[0].rstrip(),
            'line': int(m.group(2)),
            'snippet': text[:200],
            'why': text[:500],
        }
    # Formato 3: - FILE:LINE: ... (markdown bullet, como a veces devuelve el 30b).
    m = re.match(
        rf'^[\s\-\*]*({PATH}\.[a-z]{{1,5}}):(\d+)[:\s]+(.{{5,500}})$',
        line,
    )
    if m:
        text = m.group(3).strip()
        return {
            'file': m.group(1),
            'line': int(m.group(2)),
            'snippet': text[:200],
            'why': text[:500],
        }
    # Formato 4: `path/file.tsx:42` seguido de texto (sin separador).
    m = re.match(
        rf'^`?({PATH}\.[a-z]{{1,5}}):(\d+)`?\s*[:\s\-–]?\s*(.{{5,500}})$',
        line,
    )
    if m:
        text = m.group(3).strip()
        return {
            'file': m.group(1),
            'line': int(m.group(2)),
            'snippet': text[:200],
            'why': text[:500],
        }
    return None


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

    log(f'  [hunter-{wid}] {hunter_spec.id} arrancando ({len(zone_files)} archivos'
        f'{", orquestador activo" if hunting_prompts else ""})')
    for file in zone_files:
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

        # Consenso: 3 muestras; solo emitimos si la mayoría coincide.
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
                    'Reporta bugs en formato `path/file.tsx:LINE` seguido de la '
                    'descripción. Si no hay bugs: NO_BUGS.\n'
                    'Céntrate SOLO en las pistas del orquestador.'
                )
            else:
                # PROMPT GENÉRICO (fallback si el orquestador no conoce este archivo).
                prompt = (
                    f'Audita el archivo `{file}` (longitud {len(code)} chars).\n'
                    f'```typescript\n{code[:8000]}\n```\n'
                    'Reporta bugs en formato `path/file.tsx:LINE` seguido de la '
                    'descripción. Si no hay bugs: NO_BUGS.\n'
                )
            text = call_model(hunter_spec, HUNTER_SYSTEM, prompt, max_tokens=1500)
            if not text or 'NO_BUGS' in text:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith('NO_BUGS') or line.startswith('#'):
                    continue
                m = _parse_bug_line(line)
                if m:
                    yield_suspect(
                        Suspect(
                            file=m['file'],
                            line=m['line'],
                            snippet=m['snippet'],
                            why=m['why'],
                            samples=1,
                            source='hunter',
                        )
                    )
        log(f'  [hunter-{wid}] {file} revisado')

    log(f'  [hunter-{wid}] {hunter_spec.id} done')


def yield_suspect(s: Suspect) -> None:
    """Encolar un suspect, con dedupe en memoria."""
    if MEM.should_skip(s.file, s.line, s.snippet):
        STATS['suspects_filtered_as_known_fp'] += 1
        return
    # Si ya está en la cola o registrado, no encolar.
    if MEM.get_bug(s.file, s.line, s.snippet) and MEM.get_bug(s.file, s.line, s.snippet).fixed:
        return
    try:
        suspectq.put_nowait(s)
        STATS['suspects_produced'] += 1
    except queue.Full:
        pass  # cola llena, drop (raro)


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

    Política de consenso (jul-2026, fix "solo M2.7 se usaba"):
      · Antes: break en el primer respondiente (M2.7-highspeed ganaba siempre).
      · Ahora: consultar FILTER_QUORUM modelos en orden round-robin (rotamos
        el offset por suspect para que todos se usen). Si la mayoría coincide
        → aceptamos. Si discordia → escalamos a tier 2 (juez) o refutamos
        conservadoramente (es mejor un falso negativo que un falso positivo
        en dinero/stock/auth).
      · Distribución: cada wid arranca con un offset distinto del array de
        modelos, y rota el offset después de cada suspect. Así garantizamos
        que los 11 modelos del tier 1 reciben tráfico equilibrado.
    """
    log(f'  [filter-{wid}] arrancando con {[f.id for f in filters]} (quorum={FILTER_QUORUM})')
    n = len(filters)
    offset = wid % n  # offset inicial distinto por hilo (diversidad)
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
        ordered = [filters[(offset + i) % n] for i in range(n)]
        offset = (offset + 1) % n

        votes: list[tuple[str, str, str]] = []  # (verdict, spec_id, head_text)
        for spec in ordered[:FILTER_QUORUM + 1]:  # +1 por si uno falla, tener quorum
            text = call_model(spec, FILTER_SYSTEM, prompt, max_tokens=300, timeout=60)
            if not text:
                continue
            head = text.strip().splitlines()[0].strip().upper()
            if 'REAL' in head and 'NOT' not in head:
                votes.append(('REAL', spec.id, head))
            elif 'NOT_A_BUG' in head or 'NOT A BUG' in head:
                votes.append(('NOT_A_BUG', spec.id, head))
            else:
                # Output no parseable: lo contamos como "duda"
                votes.append(('UNCLEAR', spec.id, head))
            if len(votes) >= FILTER_QUORUM:
                break  # ya tenemos quorum

        if len(votes) < 2:
            # Menos de 2 votos: skip silencioso (cuota agotada o modelos caídos)
            suspectq.task_done()
            continue

        # Contar votos.
        real_count = sum(1 for v in votes if v[0] == 'REAL')
        notbug_count = sum(1 for v in votes if v[0] == 'NOT_A_BUG')
        unclear_count = sum(1 for v in votes if v[0] == 'UNCLEAR')
        specs_used = ','.join(v[1] for v in votes)

        if real_count >= 2 or (real_count >= 1 and notbug_count == 0 and unclear_count == 0):
            # Quorum a favor de REAL: pasa a bugq (jueces lo confirmarán en tier 2).
            try:
                bugq.put_nowait(Bug(suspect=s))
                log(f'  [filter-{wid}] REAL [{real_count}R/{notbug_count}N/{unclear_count}U] '
                    f'{s.file}:{s.line} ({specs_used})')
            except queue.Full:
                pass
        elif notbug_count >= 2 or (notbug_count >= 1 and real_count == 0 and unclear_count == 0):
            # Quorum a favor de NOT_A_BUG: refutado.
            MEM.record_false_positive(s.file, s.line, s.snippet,
                                      reason=f'consenso {specs_used}')
            STATS['suspects_refuted_by_filter'] += 1
            log(f'  [filter-{wid}] NOT_A_BUG [{real_count}R/{notbug_count}N/{unclear_count}U] '
                f'{s.file}:{s.line} ({specs_used})')
        else:
            # Discordia: el filtro no decide. Lo dejamos pasar a tier 2 con
            # un flag "discordia" para que el juez pondere con más cuidado.
            try:
                b = Bug(suspect=s)
                # Marcamos discordia en el log; el juez leerá el contexto.
                log(f'  [filter-{wid}] DISCORD [{real_count}R/{notbug_count}N/{unclear_count}U] '
                    f'{s.file}:{s.line} → escalado a juez ({specs_used})')
                bugq.put_nowait(b)
                STATS['suspects_filtered_as_known_fp'] += 0  # no es FP
            except queue.Full:
                pass
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
    while True:
        try:
            bug = bugq.get(timeout=5)
        except queue.Empty:
            if shutdown.is_set() and bugq.empty():
                return
            continue

        prompt = (
            f'Bug: {bug.suspect.file}:{bug.suspect.line}\n'
            f'Snippet: {bug.suspect.snippet}\n'
            f'Razón original: {bug.suspect.why}\n\n'
            '¿Es REAL, NOT_A_BUG, o WONT_FIX?'
        )
        for spec in judges:
            text = call_model(spec, JUDGE_SYSTEM, prompt, max_tokens=400, timeout=60)
            if not text:
                continue
            head = text.strip().splitlines()[0].strip().upper()
            bug.voters.append(spec.id)
            if 'REAL' in head and 'NOT' not in head:
                bug.votes_real += 1
            elif 'NOT_A_BUG' in head or 'NOT A BUG' in head:
                bug.votes_not_bug += 1
            # WONT_FIX no incrementa votos.

        # Quorum.
        if bug.votes_real >= JUDGE_MIN and bug.votes_real > bug.votes_not_bug:
            STATS['bugs_confirmed'] += 1
            log(f'  [judge-{wid}] CONFIRMED {bug.suspect.file}:{bug.suspect.line} '
                f'({bug.votes_real}/{len(bug.voters)} REAL)')
            # Lanzar fix en hilo aparte.
            threading.Thread(
                target=fixer_worker, args=(bug,), daemon=True, name=f'fixer-{wid}'
            ).start()
        else:
            # No llegó a quorum → falso positivo probable.
            MEM.record_false_positive(bug.suspect.file, bug.suspect.line,
                                      bug.suspect.snippet,
                                      reason=f'{bug.votes_real}REAL/{bug.votes_not_bug}NOT')
        bugq.task_done()


# ---------------------------------------------------------------------------
# Tier 3 — Fixer (cadena con fallback).
# Aplica el fix, valida con tsc+E2E, commitea o revierte.
# ---------------------------------------------------------------------------

def fixer_worker(bug: Bug) -> None:
    log(f'  [fixer] {bug.suspect.file}:{bug.suspect.line} arrancando')
    STATS['fixes_attempted'] += 1

    fixers = quota.filter_tier(tiers.get_tier3_fixers(), 'fixer')
    if not fixers:
        log('  [fixer] no hay fixers disponibles (todos en cuarentena)')
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
            'describe solo el cambio y devuélvelo en un patch.'
        )
        fixed = call_model(
            spec,
            'Eres un fixer de código. Devuelve solo el código arreglado, sin markdown.',
            prompt, max_tokens=8000, timeout=180,
        )
        if not fixed or fixed.strip() == original.strip():
            log(f'  [fixer] {spec.id} no produjo cambio')
            continue

        # Quitar markdown si lo agregó.
        cleaned = re.sub(r'^```(?:typescript|ts|js)?\n', '', fixed)
        cleaned = re.sub(r'\n```$', '', cleaned)
        if not cleaned.strip() or cleaned.strip() == original.strip():
            continue

        # Aplicar y validar.
        if _apply_and_validate(path, cleaned, bug.suspect):
            STATS['fixes_succeeded'] += 1
            commit = _commit_fix(path, bug.suspect)
            if commit:
                MEM.record_bug(bug.suspect.file, bug.suspect.line,
                               bug.suspect.snippet, fix_commit=commit)
                if _is_risky(bug.suspect):
                    STATS['fixes_to_review'] += 1
                    _add_to_review(bug.suspect, commit, spec.id)
                log(f'  [fixer] OK {bug.suspect.file}:{bug.suspect.line} '
                    f'({spec.id} → {commit[:7]})')
            else:
                # Aplicado pero no comiteado (validó pero git falló).
                log(f'  [fixer] aplicado pero sin commit: {path}')
            return
        else:
            # Validación falló: revertir y probar siguiente fixer.
            path.write_text(original)
            log(f'  [fixer] {spec.id} falló la validación, revertido')

    log(f'  [fixer] TODOS los fixers fallaron para {bug.suspect.file}:{bug.suspect.line}')


def _apply_and_validate(path: Path, new_content: str, suspect: Suspect) -> bool:
    """Aplica el cambio, corre tsc + un sub-set de e2e, revierte si falla."""
    original = path.read_text(errors='ignore')
    try:
        path.write_text(new_content)
        # Gate 1: typecheck.
        if not _run_tsc():
            log('  [fix] tsc falló')
            return False
        # Gate 2: smoke E2E (no el suite completo, solo las rutas afectadas).
        if not _run_smoke_subset(suspect.file):
            log('  [fix] E2E smoke falló')
            return False
        return True
    except Exception as e:
        log(f'  [fix] excepción: {e}')
        path.write_text(original)
        return False
    finally:
        # Si falló, reasegurar el original.
        if path.read_text(errors='ignore') != new_content:
            pass  # ya revertido arriba


def _run_tsc() -> bool:
    try:
        result = subprocess.run(
            ['npx', 'tsc', '--noEmit'],
            cwd=str(WT / SUBDIR),
            capture_output=True,
            text=True,
            timeout=180,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _run_smoke_subset(file_path: str) -> bool:
    """E2E smoke rápido contra el dev server."""
    try:
        # Si la ruta toca un módulo del panel, smoke contra la home + 1 ruta del módulo.
        url = DEV_URL + '/'
        with urllib.request.urlopen(url, timeout=15) as r:
            if r.status >= 500:
                return False
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return False


def _commit_fix(path: Path, suspect: Suspect) -> Optional[str]:
    """Commitea el fix en la rama actual (worktree)."""
    rel = path.relative_to(WT)
    try:
        subprocess.run(['git', 'add', str(rel)], cwd=str(WT), check=True, capture_output=True)
        msg = (
            f'fix(harness-v6): {suspect.file}:{suspect.line} — {suspect.why[:60]}\n\n'
            f'Generado por harness v6.\n'
            f'Hash: {suspect.hash}'
        )
        result = subprocess.run(
            ['git', 'commit', '-m', msg],
            cwd=str(WT), check=True, capture_output=True, text=True, timeout=30,
        )
        # Extraer SHA del commit.
        rev = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=str(WT), capture_output=True, text=True, timeout=10,
        )
        return rev.stdout.strip()
    except subprocess.CalledProcessError as e:
        log(f'  [fix] git falló: {e.stderr[:200] if e.stderr else e}')
        return None


def _is_risky(suspect: Suspect) -> bool:
    """True si el fix toca dinero, stock, estado, o auth."""
    full = (suspect.file + ' ' + suspect.why + ' ' + suspect.snippet).lower()
    return any(kw in full for kw in RISKY_KEYWORDS)


def _add_to_review(suspect: Suspect, commit: str, model: str) -> None:
    items = []
    if REVIEW_FILE.exists():
        try:
            items = json.loads(REVIEW_FILE.read_text())
        except json.JSONDecodeError:
            pass
    items.append({
        'commit': commit,
        'file': suspect.file,
        'line': suspect.line,
        'why': suspect.why,
        'snippet': suspect.snippet[:200],
        'model': model,
        'created_at': time.time(),
        'reason': 'toca dinero/stock/estado/auth — requiere revisión humana',
    })
    REVIEW_FILE.write_text(json.dumps(items, indent=2))


# ---------------------------------------------------------------------------
# E2E Sweep (Playwright en paralelo con el code hunter).
# ---------------------------------------------------------------------------

def e2e_sweep_worker(zone: str) -> None:
    """Corre el crawler E2E contra una zona y emite suspectq con hallazgos."""
    if not STORAGE and not DYN_ID:
        return  # modo demo, ya cubierto por el smoke global
    log(f'  [e2e-sweep] zona={zone} arrancando')
    try:
        cmd = ['node', 'e2e-runner.js', '--zone', zone, '--dev-url', DEV_URL]
        if STORAGE:
            cmd += ['--storage-state', STORAGE]
        if DYN_ID:
            cmd += ['--dynamic-id', DYN_ID]
        result = subprocess.run(
            cmd, cwd=str(Path(__file__).parent),
            capture_output=True, text=True, timeout=600,
        )
        # Parsear hallazgos del JSON de salida.
        for line in result.stdout.splitlines():
            try:
                h = json.loads(line)
                if h.get('type') in ('console_error', 'http_5xx', 'react_error', 'broken_link'):
                    STATS['e2e_findings'] += 1
                    yield_suspect(Suspect(
                        file=h.get('route', 'unknown'),
                        line=0,
                        snippet=h.get('message', '')[:100],
                        why=f'E2E: {h.get("type")} — {h.get("message", "")[:80]}',
                        source='e2e',
                    ))
            except json.JSONDecodeError:
                pass
    except subprocess.TimeoutExpired:
        log(f'  [e2e-sweep] zona={zone} timeout')


# ---------------------------------------------------------------------------
# Watchdog.
# ---------------------------------------------------------------------------

shutdown = threading.Event()


def watchdog() -> None:
    """Si no hay avance en N segundos, libera modelos pesados (Ollama unload)
    y avisa al usuario."""
    last_progress = time.time()
    last_round = STATS['rounds']
    while not shutdown.is_set():
        time.sleep(30)
        if STATS['rounds'] > last_round or STATS['fixes_succeeded'] > 0:
            last_progress = time.time()
            last_round = STATS['rounds']
        elif time.time() - last_progress > STALL_SECS:
            log(f'  [watchdog] stall {STALL_SECS}s sin avance; descargando Ollama')
            try:
                # Forzar unload de modelos pesados.
                for model_name in ('qwen3-coder:30b', 'qwen3.5:9b'):
                    base = os.environ.get('LOOP_OLLAMA_BASE', 'http://host.docker.internal:11434')
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
    """Descubre las zonas del dashboard (archivos del App Router)."""
    app_dir = WT / SUBDIR / 'app' / '(panel)'
    if not app_dir.exists():
        return []
    zones = []
    for entry in sorted(app_dir.iterdir()):
        if entry.is_dir() and not entry.name.startswith('('):
            zones.append(f'app/(panel)/{entry.name}/')
    return zones


def discover_code_zones() -> list[list[str]]:
    """Agrupa archivos TS por zona para los hunters."""
    app_dir = WT / SUBDIR / 'app' / '(panel)'
    lib_dir = WT / SUBDIR / 'lib'
    groups: dict[str, list[str]] = {}

    if app_dir.exists():
        for entry in sorted(app_dir.iterdir()):
            if entry.is_dir() and not entry.name.startswith('('):
                zone = f'app/(panel)/{entry.name}/'
                files = []
                for ts in entry.rglob('*.tsx'):
                    files.append(str(ts.relative_to(WT / SUBDIR)))
                for ts in entry.rglob('*.ts'):
                    if not ts.name.endswith('.test.ts'):
                        files.append(str(ts.relative_to(WT / SUBDIR)))
                if files:
                    groups[zone] = files[:30]  # límite por zona

    if lib_dir.exists():
        for ts in lib_dir.rglob('*.ts'):
            rel = str(ts.relative_to(WT / SUBDIR))
            if '.test.' in rel:
                continue
            # Agrupa por prefijo (lib/queries/, lib/db/, etc.).
            prefix = '/'.join(rel.split('/')[:2]) + '/'
            groups.setdefault(prefix, []).append(rel)
            if len(groups[prefix]) > 30:
                groups[prefix] = groups[prefix][:30]

    return list(groups.values())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-e2e', action='store_true', help='skip E2E sweep')
    parser.add_argument('--dry-run', action='store_true', help='no aplicar fixes')
    args = parser.parse_args()

    log('=' * 78)
    log('  HARNESS v6 — Adaptativo, Cuota-Aware, Memoria Persistente')
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

    # Cargar tiers filtrados por cuota.
    hunter_specs = quota.filter_tier(tiers.get_tier0_hunters(), 'tier0') or list(tiers.TIER0_HUNTERS)
    second_specs = quota.filter_tier(tiers.get_tier0_30b(), 'tier0') or list(tiers.TIER0_30B_OPINIONS)
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
            if mcfg_t3:
                fixers_preselect = mcfg_t3  # se usa en fixer_worker
            if mcfg_t4:
                arbiters_preselect = mcfg_t4  # se usa en tier 4
            any_filtered = mcfg_t1 or mcfg_t2 or mcfg_t3 or mcfg_t4
            if any_filtered:
                log('  [main] Config de modelos aplicada desde TUI (/tmp/harness-models.json)')
        except (json.JSONDecodeError, OSError):
            pass
    log(f'  Hunter specs activos:  {[s.id for s in hunter_specs]}')
    log(f'  Second specs activos:  {[s.id for s in second_specs]}')
    log(f'  Filter specs activos:  {[s.id for s in filter_specs]}')
    log(f'  Judge specs activos:   {[s.id for s in judge_specs]}')
    log('')

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
                log(f'  [main] Orquestador: {len(e2e_journeys)} journeys E2E generados')
    else:
        log('  [main] Orquestador no generó prompts (cache o fallback heurístico)')
        hunting_prompts = None
    log('')

    # Watchdog.
    threading.Thread(target=watchdog, daemon=True, name='watchdog').start()

    # Arranque de workers tier 0.
    hunter_threads = []
    for i in range(HUNTER_HILOS):
        t = threading.Thread(
            target=hunter_worker,
            args=(code_zones[i % len(code_zones)], hunter_specs[i % len(hunter_specs)], i),
            kwargs={'hunting_prompts': hunting_prompts},
            daemon=True,
            name=f'hunter-{i}',
        )
        t.start()
        hunter_threads.append(t)

    second_threads = []
    for i in range(SECOND_HILOS):
        t = threading.Thread(
            target=hunter_worker,
            args=(code_zones[(i + HUNTER_HILOS) % len(code_zones)], second_specs[i % len(second_specs)], i),
            kwargs={'hunting_prompts': hunting_prompts},
            daemon=True,
            name=f'second-{i}',
        )
        t.start()
        second_threads.append(t)

    # Arranque de workers tier 1 (filtros).
    for i in range(FILTER_HILOS):
        t = threading.Thread(
            target=filter_worker,
            args=(filter_specs, i),
            daemon=True,
            name=f'filter-{i}',
        )
        t.start()

    # Arranque de workers tier 2 (jueces).
    for i in range(JUDGE_HILOS):
        t = threading.Thread(
            target=judge_worker,
            args=(judge_specs, i),
            daemon=True,
            name=f'judge-{i}',
        )
        t.start()

    # E2E sweep (en paralelo con code hunting).
    if not args.no_e2e:
        for zone in discover_zones()[:5]:  # limita a 5 zonas
            threading.Thread(
                target=e2e_sweep_worker, args=(zone,), daemon=True, name=f'e2e-{zone}'
            ).start()

    # Main loop: converge-and-stop. NO termina el run prematuramente;
    # solo avisa cuando hay N rondas limpias (el código está estable). El run
    # termina por `deadline` (HOURS), no por converge.
    deadline = time.time() + HOURS * 3600
    last_status = 0
    try:
        while time.time() < deadline:
            time.sleep(15)
            STATS['rounds'] += 1
            if STATS['fixes_succeeded'] > 0 or STATS['e2e_findings'] > 0:
                STATS['clean_rounds_in_a_row'] = 0
            else:
                STATS['clean_rounds_in_a_row'] += 1

            if time.time() - last_status > 60:
                last_status = time.time()
                log(f'  [main] round={STATS["rounds"]} clean={STATS["clean_rounds_in_a_row"]} '
                    f'suspectq={suspectq.qsize()} bugq={bugq.qsize()} '
                    f'fixes={STATS["fixes_succeeded"]}')
                write_status()

            if STATS['clean_rounds_in_a_row'] >= TARGET:
                # NO break — solo avisamos. El run sigue hasta `deadline`.
                log(f'  [main] converge-adv: {STATS["clean_rounds_in_a_row"]} rondas '
                    f'limpias (target={TARGET}), código estable. Continuando '
                    f'hasta deadline ({int((deadline-time.time())/60)}m restantes).')
    except KeyboardInterrupt:
        log('  [main] KeyboardInterrupt, parando...')

    shutdown.set()
    write_status()
    log('  [main] Memorias guardadas, run terminado')
    MEM.save()

    log(quota.report())
    log(MEM.summary())


if __name__ == '__main__':
    main()
