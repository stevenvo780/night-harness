#!/usr/bin/env python3
"""
orchestrator.py — Orquestador inteligente de prompts para el harness v6.

Problema: los hunters escanean código a ciegas con prompts genéricos ("find bugs
in this file") → 0 bugs confirmados aunque un humano encuentra rápido.

Solución: este módulo LEE el proyecto, CONSTRUYE un mapa del dominio (rutas,
acciones, schema, auth, máquina de estados) y USA un modelo de ALTO CONTEXTO
(gemini-2.5-flash-lite, 1M ctx, $0.10/M) para GENERAR prompts DIRIGIDOS con
conocimiento del negocio.

El orquestador se ejecuta UNA VEZ al inicio del run. Los hunters reciben
prompts específicos por zona de código, no genéricos.

Arquitectura:
  1. Snapshot del proyecto: rutas, acciones, schema, auth, estado.
  2. Análisis de dominio con modelo alto contexto → `DomainMap`.
  3. Generación de `HuntingPrompt`s dirigidos por archivo/ruta.
  4. Generación de `E2EJourney`s para tests de navegación complejos.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------


@dataclass
class DomainMap:
    """Mapa del dominio extraído del proyecto."""
    modules: list[str] = field(default_factory=list)
    """Módulos detectados: ['despacho', 'pedidos', 'cartera', 'caja', ...]."""
    state_machines: dict[str, list[str]] = field(default_factory=dict)
    """Máquinas de estado: {'pedido': ['recibido','aceptado','en_cola',...]}."""
    roles: list[str] = field(default_factory=list)
    """Roles detectados: ['gerente', 'admin', 'vendedor', ...]."""
    auth_checks: list[str] = field(default_factory=list)
    """Patrones de auth encontrados: ['requireRole','requirePermission',...]."""
    risky_routes: list[str] = field(default_factory=list)
    """Rutas que tocan dinero/stock/auth."""
    forms: list[str] = field(default_factory=list)
    """Formularios Server Action detectados."""
    relations: list[str] = field(default_factory=list)
    """Relaciones entre entidades: ['pedido→cliente','despacho→ruta',...]."""


@dataclass
class HuntingPrompt:
    """Prompt dirigido para un cazador: qué buscar y dónde."""
    file: str
    line: int
    reason: str
    """Razón de dominio: por qué este archivo es sospechoso."""
    context: str = ''
    """Contexto extra para el prompt (estados, roles, relaciones)."""


@dataclass
class E2EJourney:
    """Recorrido E2E complejo multi-step."""
    name: str
    steps: list[str]
    expected: str
    module: str = ''


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

# Modelo de alto contexto para el orquestador.
# gemini-2.5-flash-lite: 1M context, $0.10/M input, probado en el pool.
# glm-5.2 vía opencode-go: también 1M context, alternativa.
ORCHESTRATOR_MODEL = os.environ.get(
    'LOOP_ORCHESTRATOR_MODEL',
    'minimax/MiniMax-M3',  # 1M ctx, barato, fiable (yo mismo). GLM-5.2 era caro.
)

# Cuánto contexto del proyecto pasar al modelo (en líneas).
# 1M tokens ≈ 750K líneas de código; con 5000 líneas tenemos sobra.
MAX_SNAPSHOT_LINES = 8000


# ---------------------------------------------------------------------------
# Snapshot del proyecto
# ---------------------------------------------------------------------------


def _read_file_safe(path: str, max_lines: int = 200) -> str:
    """Lee un archivo, devuelve vacío si no existe o es binario."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        return ''.join(lines[:max_lines])
    except (FileNotFoundError, UnicodeDecodeError, PermissionError):
        return ''


def _scan_routes(subdir: str) -> list[str]:
    """Lista todas las rutas page.tsx con su path relativo."""
    base = Path(subdir)
    pages = sorted(base.rglob('page.tsx'))
    return [str(p.relative_to(base)) for p in pages]


def _scan_actions(subdir: str) -> list[str]:
    """Lista todos los actions.ts."""
    base = Path(subdir)
    actions = sorted(base.rglob('actions.ts'))
    return [str(p.relative_to(base)) for p in actions]


def _scan_schema(subdir: str) -> str:
    """Lee el schema.ts (primeros 300 líneas)."""
    candidates = [
        Path(subdir) / 'lib/db/schema.ts',
        Path(subdir) / 'lib/db/schema.ts',
        Path(subdir) / 'lib/db/schema.ts',
    ]
    for c in candidates:
        if c.exists():
            return _read_file_safe(str(c), max_lines=400)
    return ''


def _scan_estado(subdir: str) -> str:
    """Lee el estado.ts (máquina de estados de pedidos)."""
    candidates = [
        Path(subdir) / 'lib/orders/estado.ts',
        Path(subdir) / 'lib/pedidos/estado.ts',
        Path(subdir) / 'lib/estado.ts',
    ]
    for c in candidates:
        if c.exists():
            return _read_file_safe(str(c), max_lines=200)
    return ''


def _scan_auth(subdir: str) -> str:
    """Lee el auth.ts."""
    candidates = [
        Path(subdir) / 'lib/auth.ts',
        Path(subdir) / 'lib/auth/index.ts',
    ]
    for c in candidates:
        if c.exists():
            return _read_file_safe(str(c), max_lines=150)
    return ''


def build_project_snapshot(worktree: str, subdir: str) -> str:
    """Construye un snapshot textual del proyecto para alimentar al modelo.

    Incluye:
      - Árbol de rutas (page.tsx)
      - Árbol de Server Actions
      - Schema (tablas, enums, relaciones)
      - Máquina de estados (pedidos)
      - Auth (roles, permisos, guards)
      - Resumen de componentes por módulo
    """
    root = Path(worktree) / subdir
    if not root.exists():
        return ''

    lines: list[str] = []
    lines.append('# Proyecto Deméter — Snapshot para orquestador de bugs')
    lines.append(f'## Fecha: {time.strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append('')

    # 1. Rutas
    lines.append('## Rutas (page.tsx)')
    routes = _scan_routes(str(root / 'app'))
    for r in routes:
        lines.append(f'  - {r}')
    lines.append(f'  Total: {len(routes)} rutas')
    lines.append('')

    # 2. Server Actions
    lines.append('## Server Actions (actions.ts)')
    actions = _scan_actions(str(root / 'app'))
    for a in actions:
        lines.append(f'  - {a}')
    if not actions:
        # Buscar en lib/
        lib_actions = sorted((root / 'lib').rglob('actions*.ts'))
        for la in lib_actions:
            lines.append(f'  - {la.relative_to(root)}')
    lines.append(f'  Total: {len(actions)} actions')
    lines.append('')

    # 3. Schema (primeras líneas)
    schema = _scan_schema(str(root))
    if schema:
        lines.append('## Schema (Drizzle)')
        # Extraer solo los nombres de tablas y enums para ahorrar tokens
        table_re = re.compile(r'export const (\w+) = pg(Table|Enum)\(', re.MULTILINE)
        tables = table_re.findall(schema)
        if tables:
            lines.append('  Tablas y enums:')
            for name, kind in tables:
                lines.append(f'    - {name} ({kind})')
        lines.append(f'  (schema completo: {len(schema.splitlines())} líneas)')
        lines.append('')

    # 4. Máquina de estados
    estado = _scan_estado(str(root))
    if estado:
        lines.append('## Máquina de Estados (pedidos)')
        lines.append('```ts')
        lines.append(estado[:3000])
        lines.append('```')
        lines.append('')

    # 5. Auth
    auth = _scan_auth(str(root))
    if auth:
        lines.append('## Auth / Permisos')
        lines.append('```ts')
        lines.append(auth[:2000])
        lines.append('```')
        lines.append('')

    # 6. Top de archivos por módulo (primeras 3 líneas de cada page.tsx para
    #    entender qué hace cada ruta).
    lines.append('## Contexto por ruta (primeras líneas de page.tsx)')
    for r in routes[:30]:  # limitar a 30 para no exceder tokens
        fpath = root / 'app' / r
        content = _read_file_safe(str(fpath), max_lines=8)
        lines.append(f'### {r}')
        lines.append('```ts')
        lines.append(content.strip())
        lines.append('```')
        lines.append('')

    snapshot = '\n'.join(lines)
    # Truncar si es demasiado largo.
    total_lines = len(snapshot.splitlines())
    if total_lines > MAX_SNAPSHOT_LINES:
        snapshot = '\n'.join(snapshot.splitlines()[:MAX_SNAPSHOT_LINES])
        snapshot += f'\n\n... (truncado a {MAX_SNAPSHOT_LINES} líneas)'
    return snapshot


# ---------------------------------------------------------------------------
# Análisis de dominio con modelo de alto contexto
# ---------------------------------------------------------------------------


ORCHESTRATOR_SYSTEM = """Analiza el snapshot de la app y genera hunting_prompts (bugs específicos por archivo).

Formato JSON EXACTO (sin markdown, solo JSON):
{
  "hunting_prompts": [
    {"file": "ruta/archivo.ts", "line": 42, "reason": "bug específico de dominio a revisar"}
  ],
  "e2e_journeys": [
    {"name": "nombre del flujo", "module": "modulo", "steps": ["paso1", "paso2"], "expected": "resultado esperado"}
  ]
}

CLAVES de bug-hunting:
- Auth: ¿cada server action tiene requireRole/requirePermission?
- Tenant: ¿cada query filtra por tenantId?
- Estados: ¿transiciones ilegales en la máquina de estados de pedidos?
- Validación: ¿formularios zod? ¿montos negativos? ¿stock negativo?
- Dinero: ¿cobros exceden saldo? ¿descuentos aplican correctamente?
- Carrera: ¿operaciones no atómicas en stock/cartera?

Genera 15 hunting_prompts y 5 e2e_journeys."""


def call_orchestrator(snapshot: str, timeout: int = 120) -> Optional[dict]:
    """Invoca al modelo orquestador con el snapshot y parsea el JSON."""
    if not snapshot:
        return None

    # Determinar el comando según el modelo.
    if ORCHESTRATOR_MODEL.startswith('gemini:'):
        model_name = ORCHESTRATOR_MODEL.split(':', 1)[1]
        # gemini CLI con --output-format json devuelve {session_id, response, stats}.
        cmd = ['gemini', '-m', model_name, '-p', snapshot, '--output-format', 'json']
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            outer = json.loads(result.stdout.strip())
            text = outer.get('response', '')
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            print(f'  [orch] gemini CLI falló: {e}')
            return None
    elif ORCHESTRATOR_MODEL.startswith(('opencode', 'minimax')):
        # opencode run lee de stdin (--model <id> -). Evitamos pasar el prompt
        # como argumento CLI porque el snapshot es grande (~23K chars).
        # minimax/* también se invoca por opencode (verificado), así que entra aquí
        # — antes el default minimax/MiniMax-M3 caía a None y mataba los prompts dirigidos.
        model_id = ORCHESTRATOR_MODEL.replace(':', '/', 1)
        system_prompt = ORCHESTRATOR_SYSTEM.replace('"', '\\"')
        stdin_input = f'<<SYS>>\n{system_prompt}\n<</SYS>>\n\n{snapshot}'
        try:
            result = subprocess.run(
                ['opencode', 'run', '--model', model_id, '-'],
                input=stdin_input,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            text = result.stdout.strip()
            # Filtrar líneas de build.
            text = '\n'.join(
                l for l in text.split('\n')
                if not l.startswith('[0m') and 'build ·' not in l
            ).strip()
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            print(f'  [orch] opencode CLI falló: {e}')
            return None
    else:
        return None

    # Parsear JSON del output.
    try:
        # Intentar parsear directamente.
        return json.loads(text)
    except json.JSONDecodeError:
        # Buscar bloque JSON entre ```json ... ``` o { ... }
        try:
            json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass
        # Buscar primer { y último }
        try:
            start = text.find('{')
            end = text.rfind('}')
            if start >= 0 and end > start:
                return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
        # Si el modelo devolvió JSON malformado, log + cache vacío.
        # No crashear: el caller tiene fallback heurístico.
        print(f'  [orch] respuesta NO es JSON válido ({len(text)} chars, head={text[:120]!r})')
    return None


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def generate_hunting_prompts(
    worktree: str,
    subdir: str,
    cache_path: Optional[str] = '/tmp/orch-prompts.json',
) -> list[HuntingPrompt]:
    """Genera prompts de caza dirigidos a partir del proyecto.

    Usa cache: si ya existe un archivo JSON de <1h, lo reutiliza.
    """
    if cache_path and os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < 3600:  # 1h cache
            try:
                with open(cache_path) as f:
                    cached = json.load(f)
                prompts = [HuntingPrompt(**p) for p in cached.get('hunting_prompts', [])]
                if prompts:
                    print(f'  [orch] usando cache ({len(prompts)} prompts, {age:.0f}s age)')
                    return prompts
            except (json.JSONDecodeError, TypeError):
                pass

    print('  [orch] construyendo snapshot del proyecto...')
    t0 = time.time()
    snapshot = build_project_snapshot(worktree, subdir)
    if not snapshot:
        print('  [orch] snapshot vacío — ¿worktree correcto?')
        return []
    print(f'  [orch] snapshot: {len(snapshot.splitlines())} líneas en {time.time() - t0:.1f}s')

    print(f'  [orch] llamando a {ORCHESTRATOR_MODEL} (alto contexto)...')
    t0 = time.time()
    result = call_orchestrator(snapshot, timeout=180)
    if not result:
        print('  [orch] modelo no respondió — usando fallback de prompts genéricos')
        return _fallback_prompts(worktree, subdir)
    print(f'  [orch] respuesta en {time.time() - t0:.1f}s')

    prompts_raw = result.get('hunting_prompts', [])
    prompts: list[HuntingPrompt] = []
    for p in prompts_raw:
        try:
            prompts.append(HuntingPrompt(
                file=p.get('file', ''),
                line=p.get('line', 0),
                reason=p.get('reason', p.get('why', '')),
                context=p.get('context', ''),
            ))
        except (TypeError, KeyError):
            continue

    # Guardar cache.
    if cache_path and prompts:
        try:
            with open(cache_path, 'w') as f:
                json.dump({
                    'hunting_prompts': [{'file': pp.file, 'line': pp.line,
                                          'reason': pp.reason, 'context': pp.context}
                                         for pp in prompts],
                    'e2e_journeys': result.get('e2e_journeys', []),
                }, f, indent=2)
        except OSError:
            pass

    print(f'  [orch] {len(prompts)} prompts generados + {len(result.get("e2e_journeys", []))} journeys')
    return prompts


def _fallback_prompts(worktree: str, subdir: str) -> list[HuntingPrompt]:
    """Genera prompts genéricos si el modelo falla (usa patrones heurísticos)."""
    root = Path(worktree) / subdir
    prompts: list[HuntingPrompt] = []
    # Buscar actions.ts y generar prompts por cada uno.
    for action_path in sorted(root.rglob('actions.ts')):
        rel = str(action_path.relative_to(root))
        content = _read_file_safe(str(action_path), max_lines=50)
        # Heurística: si tiene 'zod' → validación, si no → posible falta.
        has_zod = 'zod' in content or 'z.' in content
        has_auth = 'requireRole' in content or 'requirePermission' in content
        module = rel.split('/')[1] if '/' in rel else 'root'
        if not has_auth:
            prompts.append(HuntingPrompt(
                file=rel, line=1,
                reason=f'Server Action sin guarda de auth visible. ¿Está protegida contra acceso no autorizado? (módulo: {module})',
                context=f'Módulo: {module}',
            ))
        if not has_zod:
            prompts.append(HuntingPrompt(
                file=rel, line=1,
                reason=f'Server Action sin validación zod. ¿Puede recibir datos malformados? (módulo: {module})',
                context=f'Módulo: {module}',
            ))
    # Buscar page.tsx que consuman datos sin error boundary.
    for page_path in sorted(root.rglob('page.tsx')):
        rel = str(page_path.relative_to(root))
        content = _read_file_safe(str(page_path), max_lines=30)
        if 'export const dynamic' not in content:
            prompts.append(HuntingPrompt(
                file=rel, line=1,
                reason=f'Ruta sin "export const dynamic". ¿Cachea datos stale? Revisa revalidate/force-dynamic.',
                context=f'Ruta: {rel}',
            ))
    print(f'  [orch] fallback: {len(prompts)} prompts heurísticos')
    return prompts[:30]  # limitar


def generate_e2e_journeys(
    worktree: str,
    subdir: str,
    cache_path: Optional[str] = '/tmp/orch-prompts.json',
) -> list[E2EJourney]:
    """Genera recorridos E2E multi-step complejos."""
    prompts: list[E2EJourney] = []
    if cache_path and os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < 3600:
            try:
                with open(cache_path) as f:
                    cached = json.load(f)
                journeys = [E2EJourney(**j) for j in cached.get('e2e_journeys', [])]
                if journeys:
                    return journeys
            except (json.JSONDecodeError, TypeError):
                pass

    # Si no hay cache, regenerar.
    snapshot = build_project_snapshot(worktree, subdir)
    if not snapshot:
        return _fallback_e2e()

    result = call_orchestrator(snapshot, timeout=120)
    if result:
        for j in result.get('e2e_journeys', []):
            try:
                prompts.append(E2EJourney(
                    name=j.get('name', ''),
                    steps=j.get('steps', []),
                    expected=j.get('expected', ''),
                    module=j.get('module', ''),
                ))
            except (TypeError, KeyError):
                continue
    if not prompts:
        prompts = _fallback_e2e()
    return prompts


def _fallback_e2e() -> list[E2EJourney]:
    """Journeys E2E genéricos si el modelo falla."""
    return [
        E2EJourney(
            name='Pedido → Despacho completo',
            module='despacho',
            steps=[
                'Login como vendedor',
                'Crear pedido en /pedidos/nuevo/pos',
                'Seleccionar cliente con crédito aprobado',
                'Agregar 3 productos',
                'Ir a pago y seleccionar crédito',
                'Confirmar pedido',
                'Ir a /despacho/recibidos',
                'Aceptar pedido',
                'Verificar que pasa a /despacho/en-cola',
                'Enrutar pedido',
                'Despachar pedido',
            ],
            expected='Pedido completa el flujo recibido→aceptado→en_cola→enrutado→despachado sin errores',
        ),
        E2EJourney(
            name='Cobro con saldo insuficiente',
            module='cartera',
            steps=[
                'Login como admin',
                'Ir a /cartera y buscar cliente con saldo $0',
                'Intentar registrar un cobro de $50,000',
                'Verificar error de validación',
            ],
            expected='El sistema debe rechazar el cobro porque excede el saldo pendiente',
        ),
    ]


if __name__ == '__main__':
    import sys
    wt = sys.argv[1] if len(sys.argv) > 1 else '/tmp/wt-night'
    sd = sys.argv[2] if len(sys.argv) > 2 else 'apps/dashboard'
    print('=== Snapshot ===')
    snap = build_project_snapshot(wt, sd)
    print(snap[:3000])
    print('=== Fin snapshot ===')
