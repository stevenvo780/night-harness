# Demeter v7 — Documento de Diseño Maestro

**Proyecto:** night-harness (alias Demeter) · **Ruta:** `/workspace/night-harness`
**Fecha:** 2026-06-17 · **Autor:** Arquitecto líder (fusión de 5 reportes de investigación)
**Destino:** `docs/superpowers/specs/demeter-v7-master-spec.md`
**Estado del código verificado en vivo:** `harness.py` (1503 líneas), `tiers.py` (690), `orchestrator.py` (543), `quota.py` (360), `memory.py` (273), `tui.py` (1124).

---

## NOTA DE FUSIÓN — Contradicciones entre reportes, resueltas contra el código

Antes del spec, fijo las discrepancias que encontré entre los 5 reportes y verifiqué leyendo el código. **Estas correcciones son normativas: el resto del documento usa las cifras corregidas.**

| # | Discrepancia | Reporte que falla | Verdad verificada (archivo:línea) |
|---|---|---|---|
| F1 | **¿Dónde viven los 5 wrappers CLI cloud?** Reporte 5 dice "`tiers.py` (`_call_opencode` L391, `_call_gemini_cli` L452…)". | **Reporte 5 (FALSO)** | Los 5 wrappers están en **`harness.py`**: `_call_minimax` (341), `_call_opencode` (391), `_call_claude_cli` (433), `_call_gemini_cli` (452), `_call_codex_cli` (479). `tiers.py` **no tiene ningún `subprocess.run`** (grep `cwd`/`subprocess.run` en tiers.py = 0). Los `subprocess.run` correspondientes están en **harness.py:351, 409, 440, 463, 490**. |
| F2 | **Líneas exactas de los wrappers.** Reporte 1 cita `351/409/440/463/490` como las *defs*. | **Reporte 1 (impreciso)** | Esas son las líneas de los `subprocess.run`, no de las `def`. Las `def` están en 341/391/433/452/479. Para los diffs uso la línea del `subprocess.run` (donde se inyecta `cwd=`). |
| F3 | **Ruta del worktree.** Contexto compartido y Reportes 1-4 dicen `/tmp/wt-night/apps/dashboard`. | **Contexto + R1-4 (desactualizado)** | El código real: `WT = Path(os.environ.get('LOOP_WT', '/tmp/demeter-loop'))` (**harness.py:68**), `SUBDIR = os.environ.get('LOOP_SUBDIR', 'apps/dashboard')` (**harness.py:69**). El default es **`/tmp/demeter-loop/apps/dashboard`** salvo override `LOOP_WT`. Reporte 5 lo detectó correctamente. **El spec usa `WT/SUBDIR` simbólico para no hardcodear.** |
| F4 | **Causa del crash `len()` de la TUI.** Reporte 4 dice "harness escribe int, TUI hace `len()`". | **Reporte 4 (CORRECTO, confirmado)** | `write_status` escribe `'hunting_prompts': len(_current_orch_prompts)` (**harness.py:222**, **int**) y `'e2e_journeys': …` (**harness.py:223**, int). La TUI hace `len(status.get('orchestrator',{}).get('hunting_prompts', []))` (**tui.py:801-802**) → `len(int)` → `TypeError`. El default `[]` solo evita el crash cuando la clave falta; en cuanto el harness la escribe (int) **revienta**. Confirmado. |
| F5 | **VRAM del 30B residente.** Reporte 1/contexto: "≈12.3GB"; Reporte 2: "~12.3 GB GPU obs."; `tiers.py:71` comenta "10.6GB". | menor | Uso **~12.3 GB** (observación en vivo de Reportes 1/2). El comentario `tiers.py:71` ("10.6GB") está desactualizado; se corrige en Fase B. |
| F6 | **deadrate del 30B local.** Contexto: "30B GPU da timed out". Reporte 5: "deadrate=0.25, recuperable, NO muerto". | aparente | **Reporte 5 gana** (verificado: probe warm 3.0s, fix real 123.3s). El 30B **no está muerto**; el timeout del fixer es muy corto (180s) y compite por VRAM. Fix = timeout 360s + `max_tokens≤2500` + liberar VRAM. |
| F7 | **¿`num_ctx` del 7b?** Reporte 2: "context_length:4096 observado". Reporte 5 lo confirma. | consistente | Ambos coinciden. El plan fuerza `num_ctx` explícito. |
| F8 | **GPU pinning fiable.** Reporte 2 (honesto): "Ollama NO garantiza pin por request; requiere instancia por GPU". Reportes 3/4 asumen `main_gpu`/`CUDA_VISIBLE_DEVICES` por request. | **R3/R4 (optimista)** | **Reporte 2 gana.** El pin fiable de la RTX 2060 requiere **una 2ª instancia Ollama** con `CUDA_VISIBLE_DEVICES=<uuid-2060>` en otro puerto (p.ej. `:11435`), modelada como un **Node más** del fleet. `main_gpu` queda como hint best-effort. Esto unifica Fase B y C: la 2060 = "nodo" local extra. |
| F9 | **¿`tiers.ModelSpec` necesita campos nuevos?** Reporte 2: "sí, extender con `base_url`/`gpu_index`…". Reporte 3: "NO, el nodo lo resuelve el router, no va en el spec". | **Conflicto de diseño** | **Resolución arquitectónica (mía):** extiendo `ModelSpec` con campos opcionales `node`/`base_url`/`gpu_index`/`gpu_uuid`/`num_ctx` (Reporte 2) **Y** el router del fleet los puebla (Reporte 3). Son complementarios: el spec **transporta** el enrutamiento ya resuelto; el router **decide**. El `id` sigue siendo `ollama:<model>` (sin host) para no romper `_call_ollama`'s `split(':',1)[1]` (harness.py:312). Detalle en §4.1 y §5.2. |

**Hechos verificados en vivo (no en disputa), citados a lo largo del spec:**
- 5 wrappers CLI cloud sin `cwd=` (harness.py:351/409/440/463/490) → heredan `/workspace/night-harness` → `File not found .../app/(panel)/clientes/page.tsx`.
- `_parse_bug_line` (harness.py:538-596): en Formatos 2/3/4 `snippet` y `why` se asignan al **mismo `text`** (líneas 567-568, 580-581, 593-594) → Snippet==Razón; Formato 1 usa `m.group(0).split('::')[0]` (line 552). Confirmado.
- `_run_smoke_subset` (harness.py:1080-1090): solo `urlopen(DEV_URL + '/')` (line 1084) — ignora la ruta del bug.
- `e2e_sweep_worker` (harness.py:1154): pasa `--zone/--dev-url` que `e2e-runner.js` no acepta + cwd `Path(__file__).parent`.
- `_call_ollama` (harness.py:310-338): sin semáforo; `base` único de `LOOP_OLLAMA_BASE` (line 313); deriva `model_name = spec.id.split(':',1)[1]` (line 312).
- `watchdog` (harness.py:1189-1216): hardcodea unload de `qwen3-coder:30b` y `qwen3.5:9b` (line 1203), ignora la 2060.
- `write_status` (harness.py:213-236): payload plano `stats`, sin `schema_version`, `hunting_prompts`/`e2e_journeys` ya como int.

---

## 1. Resumen y objetivos

### 1.1 Qué es Demeter
Harness de auto-endurecimiento nocturno. Modelos **locales** (Ollama, 2 GPUs + CPU) y **cloud** (CLIs) cazan bugs en un repo Next.js/TypeScript objetivo, aislado en un worktree desechable (`WT/SUBDIR`, default `/tmp/demeter-loop/apps/dashboard`, harness.py:68-69). Pipeline: hunters → filters (consenso/refutación) → judges → fixer con gate (`tsc` + E2E). Sin tocar `main`.

### 1.2 Criterio de éxito — AUTO-FIX AGRESIVO (decisión de usuario, requisito)
**Éxito = el harness commitea el fix automáticamente al pasar el gate (`tsc` + E2E **dirigido a la ruta del bug**) en el worktree aislado.** No requiere merge manual a `main` (el worktree es desechable). Los fixes **riesgosos** (dinero/stock/estado/auth, `_is_risky` harness.py:1118) además van a la cola de revisión (`REVIEW_FILE`). El gate actual es **falso** para este criterio (solo pega a `/`, harness.py:1084): endurecerlo es bloqueante.

### 1.3 Diagnóstico de una frase (verificado)
**El run actual (33 suspects, 3 confirmados, 0 fixes) confirma bugs sobre datos corruptos por el parser, los manda a fixers/hunters que operan a ciegas (cwd equivocado en los 5 CLI cloud + e2e_sweep), bajo un gate E2E que solo pega a `/` y nunca valida la ruta real, con un 30B que da timeout por config (no por estar muerto) y modelos cloud zombi que atascan las colas sin cuarentena.**

### 1.4 Plan secuenciado (decisión de usuario: A→B→C→D)
- **Fase A — Pipeline:** que SÍ encuentre y arregle bugs (parser, cwd, gate real, contención GPU, cuarentena de salud).
- **Fase B — VRAM local:** `fleet.py` selecciona modelo por GPU; **usa la RTX 2060 ociosa**.
- **Fase C — Multi-host:** registro genérico de N nodos, VRAM remota declarada/inferida, health-check, ruteo; ops para exponer la laptop.
- **Fase D — TUI:** schema versionado, paneles nodos/GPUs/pipeline/embudo, contadores honestos, fix del crash y del "20/6".

### 1.5 Objetivos medibles (gates de aceptación)
1. 0 llamadas `File not found .../clientes/page.tsx` en el log tras Fase A.
2. El juez recibe `snippet` (código) ≠ `why` (prosa), sin `::` literal.
3. ≥1 fix auto-commiteado que pase `tsc` + E2E **dirigido** sobre un bug sembrado conocido.
4. La RTX 2060 recibe ≥1 hunter (modelo cargado en GPU1 visible en `/api/ps`).
5. Un nodo remoto registrado entra/sale del pool sin atascar la cola (timeout por-nodo, 1 reintento).
6. La TUI no crashea: 0 `len()` sobre escalares; contadores etiquetados (`live N/M`), nunca "20/6".

---

## 2. Arquitectura tras el cambio

### 2.1 Módulo nuevo: `fleet.py`
Responsabilidad única: **descubrir hardware (GPUs locales + nodos remotos) y planificar qué modelo corre dónde**, emitiendo `ModelSpec` ya enrutados (con `base_url`/`gpu_index`/`num_ctx`). Health-check de nodos. No invoca modelos (eso sigue en `call_model`); solo decide topología y la escribe en `/tmp/harness-fleet.json` para la TUI.

### 2.2 Responsabilidad de cada archivo tras v7

| Archivo | Responsabilidad v7 | Cambios clave |
|---|---|---|
| **`harness.py`** | Orquestador: colas, workers, `call_model` (punto único de invocación), gate, commit, watchdog, `write_status` (schema v2). | Parser JSON-first (A); `cwd=WT/SUBDIR` en 5 CLIs (A); gate E2E dirigido (A); semáforo por-modelo + cuarentena de salud (A,D); `_call_ollama` node-aware lee `base_url`/`gpu`/`num_ctx` del spec (B,C); `_build_roles`/`_build_nodes`/schema v2 (D). |
| **`fleet.py`** *(nuevo)* | Discovery de GPUs (`nvidia-smi`) + nodos (`/api/tags`,`/api/ps`); `plan_models` por VRAM; health-loop; ruteo. | Crear módulo completo (§4-5). |
| **`tiers.py`** | Catálogo estático de `ModelSpec` por tier; `resolve_command`. | Extender `ModelSpec` (frozen) con 5 campos opcionales (B); catálogo limpio (cuarentena de muertos, §8); el 30B estático queda como fallback. |
| **`orchestrator.py`** | Genera prompts dirigidos desde snapshot. | Sin cambios estructurales; el contrato de salida del hunter pasa a JSONL (A) afecta el prompt que ya inlinea código. |
| **`quota.py`** | Control de cuota cloud + `local_spend`/`call_log`. | Sin cambios estructurales; `_build_roles` consume `local_spend()` (quota.py:289) y `call_log` (quota.py:280); opcional helper `calls_since(ts)`. La cuarentena de salud (A) es un registro **nuevo** en harness, separado de la cuota. |
| **`memory.py`** | Memoria persistente de bugs/FP. | Sin cambios; el dedup del fixer (C2) consulta `MEM.get_bug(...)`. |
| **`tui.py`** | TUI curses, tabs. | Schema-guard (D); `draw_nodes_panel`/`draw_pipeline_panel`/`draw_funnel_panel`; fix `len()` (tui.py:801-802) y "20/6" (tui.py:425). |
| **`e2e-runner.js`** | Crawler Playwright. Contrato: lee **1 escenario JSON por stdin/argv[2]**, emite **1 objeto JSON** `{route,ok,errors:[{kind,where,detail}]}`. | Sin cambios; el harness debe invocarlo según su contrato real (A). |

### 2.3 Flujo de datos (v7)
```
orchestrator ──prompts──▶ hunter_worker ──JSONL bug──▶ _parse_bug_line ──Suspect(validado)──▶ suspectq
   (snapshot)              (cwd=WT/SUBDIR)   (JSON-first)   (file existe en WT)
                                                                   │
suspectq ──▶ filter_worker (consenso/refuta) ──▶ judge_worker ──CONFIRMED──▶ fixer_worker
                                                                   │            (lock por archivo,
                                                                   │             dedup, cwd correcto)
                                                            _apply_and_validate
                                                            tsc + E2E DIRIGIDO a ruta(file)
                                                                   │ pasa
                                                            _commit_fix (auto-commit) [+ review si risky]

fleet.discover_fleet ──Nodes──▶ plan_models ──ModelSpec(base_url,gpu)──▶ call_model._call_ollama
health_loop ──▶ /tmp/harness-fleet.json ──▶ TUI panel NODOS
write_status (schema v2) ──▶ /tmp/loop-v6-status.json ──▶ TUI (roles/funnel/nodes)
```

---

## 3. Fase A — Pipeline (cambios EXACTOS)

> Orden de aplicación (máximo desbloqueo primero): **A2 (cwd) → A1+A3 (datos limpios) → A5 (cuarentena salud) → A4c (gate real) → A4d (contención GPU/timeouts) → A4ab (concurrencia segura) → A6 (e2e sweep)**.

### A1 — Parser robusto: `_parse_bug_line` (harness.py:538-596) + contrato del hunter

**Defecto A1.1** (harness.py:567-568, 580-581, 593-594): en Formatos 2/3/4, `snippet=text[:200]` y `why=text[:500]` se asignan al **mismo** `text` → Snippet==Razón. El juez nunca ve código.
**Defecto A1.2** (harness.py:552): Formato 1 usa `m.group(0).split('::')[0]` → arrastra la línea completa como `file`.
**Defecto A1.3:** los `::` literales del formato viejo sobreviven cuando un filtro re-serializa `FILE::LINE::SNIPPET::REASON`.

**Cambio 1 — contrato JSONL.** En `HUNTER_SYSTEM` (bloque `FORMATO DE SALIDA`, harness.py ~525-535) reemplazar por **una línea JSON por bug**:
```
FORMATO DE SALIDA: una línea JSON por bug (JSONL), nada más.
{"file":"lib/queries/clientes.ts","line":42,"severity":"high","snippet":"<CÓDIGO literal>","why":"<1-2 frases>","repro":"<cómo dispararlo, opcional>"}
- file: ruta relativa al subdir del repo · line: entero · severity: low|medium|high|critical
- snippet: el CÓDIGO sospechoso literal (NO tu explicación) · why: 1-2 frases
Si no hay bugs: NO_BUGS
```

**Cambio 2 — parser JSON-first + fallback + saneo `::`.** Reemplazar `_parse_bug_line` (538-596):
```python
def _parse_bug_line(line: str) -> Optional[dict]:
    line = line.strip()
    if not line or line.startswith('#') or line.startswith('NO_BUGS'):
        return None
    if line.startswith('{') and line.endswith('}'):          # contrato nuevo
        try:
            d = json.loads(line)
            f = str(d.get('file', '')).strip()
            if f and 'line' in d:
                return {'file': f, 'line': int(d.get('line', 0) or 0),
                        'severity': str(d.get('severity', 'medium')).lower(),
                        'snippet': str(d.get('snippet', ''))[:200],
                        'why': str(d.get('why', ''))[:500],
                        'repro': str(d.get('repro', ''))[:300]}
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    PATH = r'[\w/.\-()]+\.[a-z]{1,5}'                          # fallback línea, grupos nombrados
    m = re.match(rf'^[\s\-\*`]*(?P<file>{PATH}):(?P<line>\d+)'
                 r'(?:\s*[:\-–—]+\s*|\s+)(?P<rest>.{5,800})$', line)
    if not m:
        return None
    rest = m.group('rest').strip().strip('`')
    if '::' in rest:                                           # des-duplica formato viejo
        parts = [p.strip() for p in rest.split('::') if p.strip()]
        snippet = parts[0][:200] if parts else ''
        why = (parts[-1] if len(parts) > 1 else parts[0])[:500] if parts else ''
    else:
        snippet, why = rest[:200], rest[:500]
    return {'file': m.group('file'), 'line': int(m.group('line')),
            'severity': 'medium', 'snippet': snippet, 'why': why, 'repro': ''}
```

**Cambio 3 — propagar `severity`/`repro` al `Suspect`.** Añadir a `@dataclass Suspect` (harness.py ~116-131): `severity: str = 'medium'` y `repro: str = ''`; pasarlos en el `Suspect(...)` de `hunter_worker` (harness.py ~679-686). Alimenta el ruteo de riesgo (A4c.4) y `_is_risky`.

### A2 — cwd correcto en los 5 CLI cloud (harness.py:351,409,440,463,490) + e2e_sweep (1160)

**Defecto A2.1 (CONFIRMADO):** ninguno de `_call_minimax`/`_call_opencode`/`_call_claude_cli`/`_call_gemini_cli`/`_call_codex_cli` pasa `cwd=`. `opencode` es agéntico: su tool `Read` abre paths relativos a su cwd = `/workspace/night-harness` → `File not found .../app/(panel)/clientes/page.tsx`. (Corrección F1: están en **harness.py**, no tiers.py.)

**Cambio 1 — constante de cwd canónico.** Junto a config (harness.py ~68):
```python
WT_SUBDIR = WT / SUBDIR     # cwd canónico para CLIs agénticos y gates
```
**Cambio 2 — `cwd=str(WT_SUBDIR)` en los 5 `subprocess.run`:** harness.py **351** (minimax), **409** (opencode), **440** (claude), **463** (gemini), **490** (codex). Ejemplo (opencode, 409-414):
```python
result = subprocess.run(
    ['opencode', 'run', '--model', opencode_model_id, full_prompt],
    capture_output=True, text=True, timeout=actual_timeout,
    cwd=str(WT_SUBDIR),        # AGÉNTICO: lee archivos del worktree real
)
```
**Cambio 3 — prompts self-contained (blindaje).** El hunter ya inlinea `code[:8000]`. El **juez** (harness.py ~907-912) y el **fixer** (harness.py ~990-998) pasan solo `file:line`: añadir el `snippet` saneado al prompt del juez y nota explícita "el archivo está en el worktree actual". Reduce dependencia del file-read agéntico aun con cwd corregido.

**Defecto A2.2 — `e2e_sweep_worker` (harness.py:1154-1162):** `cmd=['node','e2e-runner.js','--zone',zone,'--dev-url',DEV_URL]` pero `e2e-runner.js` lee `argv[2]` como **ruta a un JSON** (o stdin). `argv[2]='--zone'` → `ENOENT` → `e2e_findings:0`. Fix en A6.

### A3 — Validación de existencia en `yield_suspect` (harness.py:695-722)

**Defecto:** encola sin comprobar `(WT/SUBDIR/file).exists()`; fantasmas (`fecha`, `unknown` de e2e) llegan al fixer, que recién en harness.py:984 aborta tras gastar jueces.

**Cambio** — insertar al inicio de `yield_suspect` (antes del `should_skip`):
```python
s.file = s.file.strip().lstrip('`').lstrip('./')
if s.source != 'e2e':                          # e2e usa route URL, no path de fichero
    if not s.file or not (WT / SUBDIR / s.file).is_file():
        log(f'  [yield] descartado fantasma {s.file!r}:{s.line}')
        return
```
Para `source='e2e'`, `file` es ruta URL; ese caso lo cubre el fixer ignorando findings sin archivo real (A4c).

### A4 — Gate REAL + contención GPU

#### A4c — Gate dirigido a la ruta del bug (`_run_smoke_subset`, harness.py:1080-1090)
**Defecto C1 (CONFIRMADO):** harness.py:1084 solo abre `DEV_URL + '/'`. Un fix que rompe `/despacho` pasa el gate si `/` sigue 200. **Gate falso para auto-commit.**

**Cambio 1** — reemplazar `_run_smoke_subset`:
```python
def _route_for_file(file_path: str) -> Optional[str]:
    m = re.search(r'app/\(panel\)/([^/]+)/', file_path)
    return '/' + m.group(1) if m else None

def _run_smoke_subset(file_path: str) -> bool:
    try:                                                   # Gate 1: home nunca rota
        with urllib.request.urlopen(DEV_URL + '/', timeout=15) as r:
            if r.status >= 500: return False
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return False
    route = _route_for_file(file_path)
    if not route: return True                              # no-ruteable (lib/): tsc ya cubre
    scn = {'baseURL': DEV_URL, 'route': route, 'maxActions': 12, 'budgetMs': 45000}
    if STORAGE: scn['storageState'] = STORAGE
    try:
        res = subprocess.run(['node', 'e2e-runner.js'], cwd=str(Path(__file__).parent),
                             input=json.dumps(scn), capture_output=True, text=True, timeout=90)
        out = json.loads(res.stdout)
        fatal = [e for e in out.get('errors', []) if e.get('kind') in ('http','pageerror','errorUI')]
        if fatal:
            log(f'  [fix] E2E dirigido {route} encontró {len(fatal)} errores'); return False
        return True
    except (json.JSONDecodeError, subprocess.TimeoutExpired, Exception) as e:
        log(f'  [fix] E2E gate error: {e}'); return False   # conservador: si no corre, NO commitear
```

**Defecto C3 — `_apply_and_validate` (harness.py:1042) escribe patches como archivo completo:** `path.write_text(new_content)` (1046) asume archivo completo; si el modelo devuelve diff, `tsc` falla siempre.
**Cambio 2** — detectar diff: si `new_content.lstrip().startswith(('--- ','diff ','@@'))`, aplicar con `git apply --3way` en `cwd=WT`; si falla, `return False` sin escribir.

**Defecto C2 — sin dedup/lock por archivo:** `fixer_worker` se lanza por cada CONFIRMED; 3 fixers concurrentes sobre `RouteMatrix.tsx` leen el mismo `original` (1044) → commit perdido/corrupción.
**Cambio 3** — al inicio de `fixer_worker` (harness.py:970): dedup vía `MEM.get_bug(...).fixed` + lock por archivo (`_file_locks` con guard de módulo); si está locked, requeue. `finally: lock.release()`.

**Cambio 4 — umbral para auto-commit agresivo:** exigir además `bug.suspect.severity in ('high','critical')` **o** quorum 3/3 antes de lanzar fixer (harness.py ~944). Los `medium` van a cola de revisión, no a auto-commit. (Hoy `JUDGE_MIN=2` con jueces ruidosos confirmó snippet corrupto.)

#### A4d — Contención GPU (`_call_ollama`, harness.py:310-338)
**Defecto D1:** sin semáforo; hasta 4 hilos golpean el mismo 30B → timeout 120s. **Defecto D2:** 30B GPU + 80B CPU compiten por carga (thrashing). **Defecto D3:** RTX 2060 ignorada (resuelta en Fase B).

**Cambio 1 — semáforo por-modelo** (módulo, harness.py ~149):
```python
_OLLAMA_SEMAPHORES: dict[str, threading.Semaphore] = {}
_OLLAMA_SEM_GUARD = threading.Lock()
def _ollama_sem(model_name: str) -> threading.Semaphore:
    with _OLLAMA_SEM_GUARD:
        if model_name not in _OLLAMA_SEMAPHORES:
            limit = 1 if ('cpu' in model_name or 'next' in model_name) else 2
            _OLLAMA_SEMAPHORES[model_name] = threading.Semaphore(limit)
        return _OLLAMA_SEMAPHORES[model_name]
```
Envolver el `urlopen` de `_call_ollama` (336-338) en `with _ollama_sem(model_name):`.

**Cambio 2 — timeouts/opciones por modelo** (harness.py:322-330). Añadir a `payload['options']`: `num_ctx` (8192 default; ver Fase B por GPU) y subir el suelo del 30B GPU a 90s; **fixer del 30B → timeout 360s + max_tokens≤2500** (ver §8 y A-fixer). Mantener el suelo CPU 300s (ya en 330).

**Cambio 3 — env del host Ollama (documentar + verificar al arranque):**
```
OLLAMA_NUM_PARALLEL=2 OLLAMA_MAX_LOADED_MODELS=3 OLLAMA_KEEP_ALIVE=10m ollama serve
```
`MAX_LOADED_MODELS>=3` evita el thrashing D2. El harness lo **verifica** con el health-probe (A5).

### A5 — Health-probe + cuarentena de modelos zombi
**Defecto E1:** `north-mini-code-free`, `nemotron-3-ultra-free`, `gemini-3-flash-preview` queman 60-184s por llamada y atascan los `ThreadPoolExecutor` del filtro/juez (`as_completed(timeout=120)`). `quota.is_model_ok` (harness.py:269) solo mira cuota, no salud. **Defecto E2:** el warm solo verifica cuota.

**Cambio 1 — registro de salud** (módulo, harness.py ~155): `_HEALTH`, `_HEALTH_LOCK`, `QUARANTINE_SECS=600`, `FAIL_THRESHOLD=3`, con `_mark_health(id, ok)` e `is_healthy(id)` (cuarentena temporal por umbral de fallos).
**Cambio 2 — enchufar a `call_model`** (harness.py:269 y tras obtener texto): gatear `if not quota.is_model_ok(spec.id) or not is_healthy(spec.id): return None`; registrar `_mark_health(spec.id, ok=bool(text and text.strip() and len(text) > 8))` (el `>8` filtra los `out~1` de ~4 chars como fallo de salud).
**Cambio 3 — probe inicial + watchdog de salud** (arrancar en `main` junto al watchdog): `health_probe_all(specs)` (prompt "Responde solo OK", max_tokens=8, timeout=25) y un hilo `health_watchdog` que re-prueba cada ~5min. Como `call_model` gatea por `is_healthy`, los cuarentenados se saltan en filtro/juez/hunter sin tocar esos loops.

### A6 — Reparar `e2e_sweep_worker` al contrato real (harness.py:1154-1162)
Reemplazar el bloque por el contrato de `e2e-runner.js` (1 escenario JSON por stdin → 1 objeto JSON):
```python
scn = {'baseURL': DEV_URL,
       'route': '/' + zone.replace('app/(panel)/', '').rstrip('/'),
       'maxActions': 24, 'budgetMs': 70000}
if STORAGE: scn['storageState'] = STORAGE
result = subprocess.run(['node', 'e2e-runner.js'], cwd=str(Path(__file__).parent),
                        input=json.dumps(scn), capture_output=True, text=True, timeout=120)
try:
    res = json.loads(result.stdout)
    for err in res.get('errors', []):
        STATS['e2e_findings'] += 1
        yield_suspect(Suspect(file=res.get('route', 'unknown'), line=0,
            snippet=err.get('detail', '')[:100],
            why=f'E2E {err.get("kind")}@{err.get("where")}: {err.get("detail","")[:80]}',
            source='e2e'))
except json.JSONDecodeError:
    log(f'  [e2e-sweep] {zone} salida no-JSON: {result.stdout[:200]}')
```

### Tabla resumen Fase A
| # | Ubicación (verificada) | Defecto | Efecto |
|---|---|---|---|
| A1 | harness.py:567-568,580-581,593-594 | snippet=why=text; `::` literal | juez recibe Snippet==Razón |
| A1 | harness.py:552 | F1 `m.group(0).split('::')[0]` | file ruidoso |
| A2 | harness.py:351,409,440,463,490 | sin `cwd=` (¡en harness.py!) | `File not found .../clientes/page.tsx`, 80× `out~1` |
| A2 | harness.py:1154-1162 | argv `--zone` que e2e-runner no acepta | `e2e_findings:0` |
| A3 | harness.py:695-722 | no valida file existe | fantasmas al fixer |
| A4c | harness.py:1084 | solo pega a `/` | gate falso |
| A4c | harness.py ~944,969 | sin dedup/lock | 3 fixers sobre RouteMatrix |
| A4c | harness.py:1046 | patch escrito como archivo | tsc falla |
| A4d | harness.py:310-338 | sin semáforo | 30B timeout |
| A5 | harness.py:269,1304 | sin cuarentena de salud | zombis atascan colas |

---

## 4. Fase B — Selección por VRAM local (`fleet.py`, parte 1)

**Hardware verificado en vivo:** GPU0 RTX 5070 Ti 16303 MiB (~9 GB libres, 30B residente ~12.3 GB); GPU1 RTX 2060 6144 MiB (205 MiB usados, **ociosa**). `qwen2.5-coder:7b` cargado con `size_vram`≈5.0 GB → el 7b cabe en la 2060, el 14b (9.3 GB) **no**.

### 4.1 Extender `tiers.ModelSpec` (tiers.py:46, `frozen=True`) — campos opcionales
Resolución F9: añadir AL FINAL de `ModelSpec` (compatible con `frozen` y todos los call-sites posicionales):
```python
base_url: Optional[str] = None     # None => LOOP_OLLAMA_BASE (comportamiento actual)
gpu_index: Optional[int] = None    # GPU a pinear
gpu_uuid: Optional[str] = None
node: Optional[str] = None         # nombre del Node (logging/diversidad)
num_ctx: Optional[int] = None      # contexto a pedir a Ollama (para caber en VRAM)
```
**El `id` sigue siendo `ollama:<model>` sin host** → no rompe `_call_ollama`'s `split(':',1)[1]` (harness.py:312) ni `resolve_command` (tiers.py:609, que lanza `ValueError` para `ollama:` por diseño, correcto: Ollama va por HTTP).

### 4.2 Dataclasses de `fleet.py`
`Gpu(index,name,vram_total_mib,vram_free_mib,util_pct,uuid,assigned_model,assigned_role)`, `CpuSlot(cores,ram_free_mib,assigned_model,assigned_role)`, `Node(name,base_url,online,gpus,cpu,models,running,is_local)`. (Detalle de campos en §5 para multi-host.)

### 4.3 Discovery local
- `discover_local_gpus()`: `nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu,uuid --format=csv,noheader,nounits`. Salida real hoy: `[Gpu(0,5070Ti,16303,free≈9033,util=4), Gpu(1,2060,6144,free≈5939,util=1)]`. Si `returncode!=0` → `[]` (host CPU-only).
- `discover_models(base_url)`: `GET /api/tags` → `{name: size_bytes}` (filtra embeddings).
- `discover_running(base_url)`: `GET /api/ps` → `{name: size_vram}` (qué está caliente).

### 4.4 Planificador `plan_models(node)`
```python
HEADROOM = 0.80        # usar 80% del VRAM libre
VRAM_OVERHEAD = 1.10   # VRAM ≈ size_disco × 1.10 (validado: 7b 4.68GB→5.09GB obs.)
def est_vram_mib(size_bytes): return int(size_bytes/(1024*1024)*VRAM_OVERHEAD)
SMALL_HUNTER_PREF = ['qwen2.5-coder:7b-32k','qwen2.5-coder:7b','qwen3.5:9b','qwen2.5:3b-16k']
BIG_HUNTER_PREF   = ['qwen3-coder:30b-32k','qwen3-coder:30b','devstral:24b','gpt-oss:20b','qwen3:14b-32k']
CPU_REASONER_PREF = ['qwen3-coder-next:32k','qwen3-coder-next:cpu','qwen3-coder-next:q4_K_M']
```
Algoritmo: ordenar GPUs de mayor a menor VRAM total; **rank 0 (5070 Ti) → hunter_fixer** (modelo grande), **rank ≥1 (2060) → hunter** (modelo chico). `_best_fitting(pref, installed, budget, exclude)` toma el primer modelo de preferencia que quepa; fallback al instalado más grande que entre (excluye `next`/`embed`). CPU → 80B `reasoner`.

**Refinamientos obligatorios (de §3.4/§8 del Reporte 2):**
1. **Budget de modelo ya-caliente:** si un modelo está en `/api/ps` de esa GPU, su budget = `vram_free + vram_de_ese_modelo` (no hay recarga) → el 30B se mantiene residente en el 5070 Ti.
2. **GPU casi-vacía:** usar `min(vram_free, vram_total×HEADROOM)` cuando la GPU está vacía (evita el falso negativo del 7b en la 2060: 5.1 GB est. vs 5.9 GB total → con `num_ctx=8192` entra).
3. **`num_ctx` por GPU:** `8192` si `vram_total < 8000` (2060), `32768` si no.

**Resultado concreto hoy:** 5070 Ti → `qwen3-coder:30b` (hunter_fixer, residente); 2060 → `qwen2.5-coder:7b-32k` (hunter, `num_ctx=8192`, pin vía instancia §5); CPU → `qwen3-coder-next` (reasoner, **pero cuarentenado por §8** — deadrate 1.0 bajo carga; queda como reasoner solo si pasa probe).

### 4.5 Fábrica de specs + ruteo a `_call_ollama`
`_make_ollama_spec(node, model_name, gpu, role)` → `ModelSpec(id=f'ollama:{model_name}', base_url=node.base_url, gpu_index=gpu.index, num_ctx=…, node=node.name, …)`.
`_call_ollama` (harness.py:310) lee del spec: `base = getattr(spec,'base_url',None) or LOOP_OLLAMA_BASE`; `options['num_ctx']=spec.num_ctx`; `options['main_gpu']=spec.gpu_index` (hint best-effort).

### 4.6 Integración en `harness.py`
- **harness.py ~1319-1321** (carga tier0): `FLEET=fleet.discover_fleet(); fleet_specs=[…plan_models(n)…]; fleet_specs=fleet.probe_and_quarantine(fleet_specs)`. `hunter_specs = gpu_hunters + small_hunters` (or fallback a `tiers.get_tier0_hunters()`); `second_specs = reasoners`. El loop de spawn (harness.py ~1402-1423, `hunter_specs[i % len]`) **no cambia** → la 2060 entra por round-robin. Subir `HUNTER_HILOS` default a ≥`len(hunter_specs)` para garantizar ≥1 hilo a la 2060.
- **Fixer (harness.py ~976):** anteponer el fixer local del fleet (30B en 5070, con `num_ctx`/`base_url` verificados por probe) y quitar el `ollama:` estático de `cloud_fixers`. Arregla "el 30B da timed out en el fixer".
- **Watchdog (harness.py:1203):** descargar TODOS los modelos del fleet por nodo (no hardcodear 2 modelos / 1 base).
- **Log de arranque (harness.py ~1283):** `log(f'  Fleet: {[(s.node, s.gpu_index, s.id) for s in fleet_specs]}')`.

---

## 5. Fase C — Multi-host (registro N-nodos, VRAM remota, health, ruteo, ops)

**Reachability verificada:** localhost (`host.docker.internal:11434`) `/api/tags`+`/api/ps` OK (16 modelos, `size_vram` por modelo); laptop `192.168.78.135` pingea 0.5 ms pero `:11434` REFUSED (Ollama atado a 127.0.0.1), SSH:22 responde. **Ollama NO expone VRAM total por API** (solo `/api/ps.models[].size_vram`).

### 5.1 Config (env, sin hardcodear ninguna máquina)
```
LOOP_NODES='localhost=http://host.docker.internal:11434,laptop=http://192.168.78.135:11434'
LOOP_NODE_META='laptop:vram=8192:gpus=1:roles=hunter,filter:timeout_mult=2.0;localhost:roles=*'
LOOP_NODE_PROBE_TIMEOUT=3   LOOP_NODE_HEALTH_INTERVAL=20
LOOP_NODE_FAIL_THRESHOLD=2  LOOP_NODE_RECOVER_THRESHOLD=2
```
Si `LOOP_NODES` falta → un único nodo `localhost` desde `LOOP_OLLAMA_BASE` (harness.py:313) → cero cambios para quien no use multi-host. **La RTX 2060 se modela como un nodo local extra** (resolución F8): `localhost-2060=http://host.docker.internal:11435` apuntando a una 2ª instancia Ollama pineada.

### 5.2 VRAM efectiva por nodo (cascada)
```python
def node_vram_total_mib(node):
    if node.is_local and node.gpus: return sum(g.vram_total_mib for g in node.gpus)  # nvidia-smi
    if node.declared_vram_mib is not None: return node.declared_vram_mib              # LOOP_NODE_META
    return max(node.vram_residente_mib, _largest_loaded_ever(node), 2048)             # inferencia
```
`vram_free = total - vram_residente` (suma de `size_vram` de `/api/ps`, funciona local Y remoto sin `nvidia-smi` remoto).

### 5.3 Health-check (hilo demonio, con histéresis)
`health_loop` corre `probe_node` (GET `/api/tags`+`/api/ps`, mide RTT) cada `HEALTH_INTERVAL`, escribe `/tmp/harness-fleet.json` (lo lee la TUI). `_mark_fail`/`_mark_up` con `FAIL_THRESHOLD`/`RECOVER_THRESHOLD` (anti-flapping). No bloquea el pipeline: workers leen `fleet.up_nodes()` (lectura lock-free de `status`).

### 5.4 Política de ruteo (orden de prioridad)
1. **El FIXER nunca sale de localhost** (necesita worktree, `tsc`, E2E en disco local). `nodes_for_role('fixer')` = `node.is_local and 'fixer' in roles`.
2. **Capability gate:** solo nodos con `spec_model_name in node.models` (`/api/tags`). Desacopla `tiers.py` de la topología.
3. **Role gate:** el nodo declara el rol (o `*`). Remotos por defecto toman `hunter`+`filter`; jueces/fixer local salvo override.
4. **Selección por VRAM libre:** `score = (cabe, is_local, free, -rtt_ms)`; `pick_gpu` asigna 30B→5070Ti, 7B/3B→2060.
5. **Anti-contención local:** no meter un 2º modelo grande en GPU0 si `vram_free < need`; overflow → 2060 → remoto → CPU.
6. **Balanceo:** empates round-robin por `node.inflight` (++ al rutear, -- en `finally` de `call_model`).

Invocación: el router se llama **dentro de `call_model`** (harness.py:262) cuando `spec.id.startswith('ollama:')`, devuelve `(node, gpu)`, y `_call_ollama` recibe `node.base_url` + `node.timeout_mult`. **`ModelSpec.id` no cambia** (F9: el nodo viaja en atributos resueltos por el router; sin host en el id).

### 5.5 Modos de fallo (no atascar la cola)
Timeout **por-nodo** (`timeout*mult`), **máx 1 reintento** a otro nodo (el consenso de hunters absorbe la pérdida). Nodo nunca alcanzable → nace `down`, no bloquea arranque. Nodo cae a mitad → `fail_streak≥THRESHOLD` → excluido. Modelo no instalado → capability gate lo descarta. VRAM no cabe → CPU/diferir, nunca OOM forzado. Todos los nodos down → degrada a solo-CPU-local o solo-cloud, sin deadlock. Flapping → cooldown.

### 5.6 OPS — exponer la laptop (192.168.78.135)
**Método persistente (systemd override en la laptop):**
```bash
sudo systemctl edit ollama.service   # añade:
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_ORIGINS=*"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
Environment="OLLAMA_KEEP_ALIVE=10m"
sudo systemctl daemon-reload && sudo systemctl restart ollama.service
sudo ss -tlnp | grep 11434           # debe ser 0.0.0.0:11434, NO 127.0.0.1
```
**Firewall:** `firewall-cmd --add-port=11434/tcp` / `ufw allow from 192.168.78.0/24 to any port 11434 proto tcp`.
**Verificar desde el contenedor:** `curl -sS --max-time 3 http://192.168.78.135:11434/api/tags` y `/api/ps`; smoke `/api/generate`.
**Atajo sin tocar la laptop (SSH:22 responde):** `ssh -fN -L 11534:127.0.0.1:11434 user@192.168.78.135` y registrar `laptop=http://127.0.0.1:11534`.
**Registrar:** exportar `LOOP_NODES`/`LOOP_NODE_META` (§5.1); el health-loop la detecta UP en <20s.

---

## 6. Fase D — TUI (schema versionado, paneles, contadores honestos)

### 6.1 Bugs verificados
| # | Síntoma | Causa raíz (verificada) |
|---|---|---|
| 1 | "20/6" sin sentido | `_show_model_config` muestra `({len(selected)}/{len(all_models)})` (**tui.py:425**) sin etiqueta de rol. |
| 2 | crash `len()` sobre int | `write_status` escribe `hunting_prompts` int (**harness.py:222**); TUI hace `len(...get('hunting_prompts', []))` (**tui.py:801-802**). El `try/except curses.error` (tui.py:807) NO captura `TypeError` → revienta el render del dashboard. |
| 3 | dashboard ignora nodos/GPUs/VRAM | `get_gpu_stats`/`get_ollama_models` (tui.py:313/337) solo en `draw_system_panel` (591) como apéndice; sin registro de nodos; 2060 nunca asociada a modelo. |
| 4 | `kind` mezcla rol+ubicación | `hunter_worker` escribe `kind='cloud'`/`'hunter'` (rol hunter en ambos); TUI los separa por `kind` (tui.py:744-754). |
| 5 | sin schema versionado | `write_status` (harness.py:215) no escribe `schema_version`; la TUI no detecta payloads viejos. |

### 6.2 Layout dashboard: 3 paneles apilados (NODOS → PIPELINE → EMBUDO)
- **Panel NODOS** (`draw_nodes_panel`, NUEVA): por nodo (● alive/○ down, host, latency); por GPU (`GPUi name util% vram U/T [barra] ~modelo`); línea ollama. **La 2060 aparece como GPU1 `(libre)` si `vram_used<500MiB`.** Fusiona `get_gpu_stats()` (nvidia-smi local) en `nodes[id=="local"].gpus` en render.
- **Panel PIPELINE** (`draw_pipeline_panel`, reescribe `draw_process_containers` tui.py:727): itera `status['roles']` (pre-calculado), línea por rol `{icon} {label} live {live_n}/{configured_n} {iconos} {calls_total}c {calls_per_min}/min to:{timeouts} q:{quarantined_n}`. **Contadores etiquetados → mata "20/6".** Sub-líneas `local …`/`cloud …` por eje `location`; `└ busy …` por `role`+`status=='busy'`. **Orquestador: leer `hunting_prompts_n`/`e2e_journeys_n` como int directo — eliminar las dos `len()` de tui.py:801-802.**
- **Panel EMBUDO** (`draw_funnel_panel`, NUEVA): lee `status['funnel']`; `suspects ─▶ refutados ─▶ FP ─▶ CONFIRMADOS`; `fixes att/OK/rev/review`, `e2e/opus`; mini-barras normalizadas.

### 6.3 Otros cambios `tui.py`
- `read_status` (256): guard de versión `if sv != SCHEMA_VERSION: data['_schema_warning']=…`; helpers `_funnel`/`_roles` con defaults seguros.
- `draw_header` (502): compactar a `Uptime │ Round │ Clean` + badge `schema vN · ts {edad}s`; leer de `funnel` (no `stats`).
- `_show_model_config` (425): `f'{tlabel} — {tdesc} · {len(selected)} de {len(all_models)} activos'`.
- `main` render dashboard (~1068-1071): llamar los 3 paneles nuevos.
- **Invariante anti-crash:** ningún consumidor de la TUI llama `len()` sobre un campo del status; todos los conteos llegan como int con sufijo `_n`/`_count`/`_total`. La TUI solo itera `roles`/`threads`/`nodes`/`gpus` (listas) y lee escalares en `funnel`/`orchestrator`.

### 6.4 Cambios correspondientes en `harness.py`
- `update_thread_state` (186): añadir ejes `role`/`location`/`node` (mantener `kind` por compat). Setearlos en `hunter_worker` (599), `filter_worker` (750), `judge_worker` (893), `fixer_worker` (969).
- **NUEVA `_build_roles()`**: recorre `THREAD_STATES` + `quota.local_spend()` → lista `roles` (configured_n de `HUNTER/FILTER/JUDGE/FIXER_HILOS`; live/busy/idle/done; quarantined_n; calls_total; calls_per_min; timeouts; errors; models).
- **NUEVA `_build_nodes()` / `_probe_node()`**: esqueleto local (`/api/ps`+latencia, `gpus:[]` que la TUI rellena); Fase C lee `/tmp/harness-fleet.json` (genérico, sin hardcodear laptop).
- **`write_status` (213) → schema v2** (§7).

---

## 7. STATUS SCHEMA v2 (fuente de verdad compartida harness↔tui)

Archivo: `/tmp/loop-v6-status.json`. Producido por `write_status` (harness.py:213), consumido por `tui.py`. **Regla de oro: ningún campo que la TUI haga `len()` puede ser int; los conteos van pre-calculados con sufijo `_n`/`_count`/`_total`.**

```jsonc
{
  "schema_version": 2,
  "ts": 1718600000.0,
  "pid": 12345,
  "uptime_sec": 3600,
  "funnel": {                                  // int 1:1 con STATS (harness.py:155-169)
    "suspects_produced": 33, "suspects_refuted": 18, "suspects_known_fp": 4,
    "bugs_confirmed": 3, "fixes_attempted": 0, "fixes_succeeded": 0,
    "fixes_reverted": 0, "fixes_to_review": 0, "e2e_findings": 0,
    "opus_invocations": 0, "rounds": 5, "clean_rounds_in_a_row": 1
  },
  "roles": [                                   // una entrada por ROL (no por kind crudo)
    {"role":"hunter","label":"Hunters","configured_n":6,"live_n":6,"busy_n":4,
     "idle_n":2,"done_n":0,"quarantined_n":0,"calls_total":120,"calls_per_min":12.3,
     "timeouts":2,"errors":1,"models":["qwen3-coder:30b","qwen2.5-coder:7b-32k"]}
    // filter, judge, fixer, arbiter
  ],
  "threads": [                                 // drill-down; ejes nuevos role/location/node
    {"name":"hunter-0","role":"hunter","location":"local","node":"gpu0",
     "model":"ollama:qwen3-coder:30b","status":"busy",
     "current_file":"app/(panel)/clientes/page.tsx","current_task":"round 2/3",
     "calls":20,"errors":0,"avg_ms":2100,"age_sec":412,"idle_sec":3}
  ],
  "orchestrator": {                            // FIX crash: int directo, sufijo _n
    "model":"ollama:qwen3:14b","hunting_prompts_n":30,"e2e_journeys_n":2,"ready":true
  },
  "nodes": [                                   // forward-compatible multi-host (Fase C)
    {"id":"local","host":"host.docker.internal:11434","alive":true,"latency_ms":4,
     "gpus":[
       {"index":0,"name":"RTX 5070 Ti","vram_used_mib":12595,"vram_total_mib":16384,
        "util_pct":18,"loaded_models":["qwen3-coder:30b"]},
       {"index":1,"name":"RTX 2060","vram_used_mib":205,"vram_total_mib":6144,
        "util_pct":0,"loaded_models":[]}],
     "ollama_models":[{"name":"qwen3-coder:30b","vram_gb":12.6,"gpu_index":0}]}
  ],
  "quota": { "...": "quota.local_spend()" },
  "memory": {"total_rounds":0,"total_bugs_found":0,"total_fixes_applied":0,
             "open_bugs":0,"hardened_zones":0}
}
```

**Contrato productor↔consumidor:**
| Campo | Productor (harness) | Consumidor (tui) | Tipo | Reemplaza |
|---|---|---|---|---|
| `schema_version` | write_status | read_status guard | int=2 | (nuevo) |
| `funnel.*` | write_status←STATS | draw_funnel_panel | int | `stats` plano |
| `roles[]` | `_build_roles` | draw_pipeline_panel | list | reagrupado por `kind` |
| `roles[].*_n` | `_build_roles` | contadores honestos | int/float | el "20/6" |
| `threads[].role/location/node` | update_thread_state | drill-down | str | `kind` ambiguo |
| `orchestrator.hunting_prompts_n` | write_status (int) | int directo | **int** | `hunting_prompts`+`len()` crash |
| `nodes[]` | `_build_nodes`+TUI merge | draw_nodes_panel | list | `draw_system_panel` apéndice |

---

## 8. Pool de modelos verificado + política de cuarentena/timeouts

### 8.1 Modelos LOCALES (latencia medida en vivo)
| Modelo | Tamaño | Latencia probe | Estado VRAM | Timeout rec. | Rol |
|---|---|---|---|---|---|
| `qwen2.5:3b` | 1.9 GB | 1.6s cold | holgado | 30s | filtro / 2060 |
| `qwen2.5-coder:7b(-32k)` | 4.7 GB | 0.16s warm | **cabe 2060** | 45s | filtro/hunter (2060) |
| `qwen3.5:9b` | 6.6 GB | ~6-8s est. | cabe 5070, NO 2060 | 60s | filtro/hunter |
| `qwen3:14b` | 9.3 GB | 10.1s cold | NO 2060 | 90s | hunter/juez local |
| `gpt-oss:20b` | 13.8 GB | 9.5s cold | cabe justo | 90s | hunter/juez |
| `devstral:24b` | 14.3 GB | 14.3s cold | cabe justo | 120s | fixer alterno |
| **`qwen3-coder:30b`** | 18.6 GB | **3.0s warm / fix real 123s** | residente 5070 (~12.3GB) | **360s fixer / 120s juez** | **FIXER principal** |
| `qwen3:32b` | 20.2 GB | 26.1s (spill a CPU, solo 10.3GB VRAM) | offload CPU | evitar | — |
| `qwen3-coder-next:cpu` (80B) | 51.7 GB | 76-194s, **deadrate=1.0** | CPU puro | **CUARENTENA** | inutilizable bajo carga |

### 8.2 Modelos CLOUD (deadrate del log; cuota gastada: 0)
**VIVOS ⭐:** `minimax/MiniMax-M3` (0.00), `opencode-go:deepseek-v4-pro` (0.00), `opencode-go:kimi-k2.7-code` (0.00), `minimax/MiniMax-M2.5-highspeed` (0.00), `claude:claude-haiku-4-5` (0.00, 1 muestra), `opencode:big-pickle` (0.20, degradado).
**CUARENTENA DURA (deadrate≥0.5 o timeout sistemático):** `gemini:gemini-3-flash-preview` (1.00), `minimax/MiniMax-M2.7-highspeed` (0.75), `gemini:gemini-2.5-flash-lite` (0.67), `opencode:mimo-v2.5-free` (0.53), `ollama:qwen3-coder-next:cpu` (1.00).
**CUARENTENA BLANDA (solo último fallback):** `opencode:deepseek-v4-flash-free` (0.41), `opencode:north-mini-code-free` (0.37, 4 timeouts 180s), `opencode:nemotron-3-ultra-free` (0.29, med 155s).
**DESCONOCIDOS (no cuarentenar):** `gemini-2.5-pro`, `gemini-3-pro-preview`, `codex:gpt-5.5`, `claude:claude-sonnet-4-5`, `opencode-go:glm-5.2`, `opencode-go:qwen3.7-max`.

### 8.3 Pool por tier
- **tier0 hunters:** LOCAL `qwen2.5-coder:7b`(2060), `qwen3-coder:30b`(5070), `qwen3:14b`, `gpt-oss:20b`; CLOUD `MiniMax-M3`, `deepseek-v4-pro`, `claude-haiku-4-5`. Timeout local 60-90s, cloud 60s.
- **tier1 filtros:** LOCAL `qwen2.5-coder:7b`+`qwen2.5:3b`(2060); CLOUD `MiniMax-M2.5-highspeed`, `deepseek-v4-pro`, `big-pickle`. Quitar todos `*-free` y `*highspeed` de deadrate alto.
- **tier2 jueces:** CLOUD `MiniMax-M3`⭐, `kimi-k2.7-code`, `deepseek-v4-pro`; LOCAL respaldo `qwen3:14b`, `gpt-oss:20b`. No usar gemini-flash-*.
- **tier3 fixers:** LOCAL principal `qwen3-coder:30b` (**timeout 360s + max_tokens≤2500 + liberar VRAM del 7b**); alterno `devstral:24b`; CLOUD fallback `deepseek-v4-pro`, `kimi-k2.7-code`, `MiniMax-M3`. **Pre-requisito: cwd corregido (A2).**

### 8.4 Timeouts a aplicar en código
- `harness.py:1002` (fixer call): `timeout=180→360`, `max_tokens=8000→2500`.
- `harness.py:326-330` (`_call_ollama`): timeout dinámico por tamaño (≥24GB→360s; CPU ya tiene 300s en 330).
- `harness.py:408` (`_call_opencode actual_timeout=180`): los `*-free` lo agotan → **cuarentenar**, no subir.
- Cuarentena alimentada por A5 (`_HEALTH`) + lista estática inicial §8.2 en `tiers.py`/quota.

---

## 9. Plan de VERIFICACIÓN por fase (cómo PROBAMOS que encuentra y arregla un bug real)

### Pre-flight (común)
```bash
# Worktree y dev server vivos:
ls /tmp/demeter-loop/apps/dashboard/app/\(panel\)/clientes/page.tsx   # archivo real existe
curl -sf "$DEV_URL/" -o /dev/null && echo "dev up"
curl -s http://host.docker.internal:11434/api/tags | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["models"]),"modelos")'
```

### Fase A
1. **cwd:** correr 1 ronda; `grep -c "File not found" /tmp/loop-v6.log` debe ser **0** (antes >0). `grep -c "out~1" /tmp/loop-v6.log` debe caer drásticamente.
2. **parser:** unit `python3 -c "import harness; print(harness._parse_bug_line('{\"file\":\"lib/x.ts\",\"line\":42,\"snippet\":\"const a=b\",\"why\":\"falta scope\"}'))"` → snippet≠why; y sobre `'app/(panel)/caja/CajaTabs.tsx:105 const activo = t.id===tab;::Posible...'` → sin `::` literal, snippet≠why.
3. **fantasmas:** sembrar un suspect `file='fecha'` → log `[yield] descartado fantasma`.
4. **gate real:** sembrar un bug que rompe `/despacho` pero no `/`; el fixer NO debe auto-commitear (E2E dirigido falla). Sembrar un fix correcto → `_run_smoke_subset` pasa y commitea.
5. **bug end-to-end sembrado:** introducir un bug TS conocido (p.ej. `const x: number = "str"` en un archivo ruteable) → esperar `fixes_succeeded≥1` + `git -C /tmp/demeter-loop log --oneline -1` muestra `fix(harness-v6)`. **Este es el gate de éxito del criterio de auto-fix agresivo.**
6. **cuarentena salud:** tras 3 timeouts de `north-mini-code-free` → log `[health] … CUARENTENA`; ese id deja de aparecer en `[call]`.
7. **e2e sweep:** `e2e_findings` en status > 0 cuando hay error real en una zona.

### Fase B
```bash
# Tras arrancar: la 2060 debe tener un modelo cargado.
curl -s http://host.docker.internal:11434/api/ps | python3 -c 'import sys,json;[print(m["name"],m.get("size_vram",0)//2**20,"MiB") for m in json.load(sys.stdin)["models"]]'
nvidia-smi --query-gpu=index,memory.used --format=csv,noheader   # GPU1 > 205MiB usado
grep "Fleet:" /tmp/loop-v6.log     # muestra (node, gpu_index, id) con gpu_index=1 para el 7b
```
Criterio: ≥1 hunter en GPU1; el 30B sigue residente en GPU0 sin re-descarga.

### Fase C
```bash
# Con la laptop expuesta (o túnel SSH):
export LOOP_NODES='localhost=http://host.docker.internal:11434,laptop=http://192.168.78.135:11434'
curl -sS --max-time 3 http://192.168.78.135:11434/api/tags | head -c 100   # no "refused"
cat /tmp/harness-fleet.json | python3 -m json.tool   # laptop status 'up', RTT, modelos
```
Criterio: el health-loop marca la laptop UP en <20s; el router le envía hunters/filtros; si se mata Ollama en la laptop, sale del pool sin atascar la cola (verificar que las cazas en vuelo se re-encolan 1 vez).

### Fase D
```bash
python3 -c "import tui,json; tui.read_status('/tmp/loop-v6-status.json')"   # sin TypeError
python3 -c "import json;d=json.load(open('/tmp/loop-v6-status.json'));assert d['schema_version']==2;assert isinstance(d['orchestrator']['hunting_prompts_n'],int)"
```
Criterio: TUI renderiza el dashboard sin crash; panel NODOS muestra la 2060; contadores `live N/M` (nunca "20/6"); embudo con cifras de `funnel`.

---

## 10. Riesgos y preguntas abiertas

1. **Pin de GPU fiable (F8):** Ollama no garantiza GPU por request; requiere 2ª instancia (`CUDA_VISIBLE_DEVICES=<uuid-2060> OLLAMA_HOST=127.0.0.1:11435 ollama serve`) modelada como nodo. Sin ella, la 2060 podría no recibir el 7b. **Recomendación: lanzar la 2ª instancia.** Pregunta abierta: ¿quién la arranca (systemd, script de bootstrap del harness)?
2. **Budget del 7b en la 2060 (5.1 GB est. vs 5.9 GB):** ajustado; depende de `num_ctx=8192` y de `HEADROOM` sobre `vram_total` para GPU vacía. Riesgo de OOM si Ollama no respeta `num_ctx`.
3. **80B CPU reasoner cuarentenado (deadrate 1.0 bajo carga):** `plan_models` lo prefiere como reasoner, pero §8 lo cuarentena. **Decisión:** el reasoner CPU solo entra si pasa `probe_model`; si no, no hay segunda-opinión CPU (aceptable: el 30B y cloud cubren).
4. **`OLLAMA_NUM_PARALLEL`/`MAX_LOADED_MODELS` del host:** el harness los verifica pero no los setea (corren fuera del contenedor). Si el operador no los configura, persiste algo de thrashing.
5. **Auto-commit agresivo + gate E2E que no corre:** la regla conservadora (si el gate no corre, NO commitear) puede bloquear todos los fixes si Playwright/dev-server fallan. Mitigar con health-check del dev server al arranque.
6. **`git apply --3way` para patches (C3):** si el modelo mezcla diff + prosa, la heurística de detección puede fallar. Preferir forzar "archivo completo" en el prompt del fixer salvo archivos >200 líneas.
7. **Líneas exactas movibles:** las cifras de línea son del estado actual (harness.py 1503 líneas); cada edición las corre. Aplicar los cambios por **función + ancla de texto**, no por número de línea fijo.
8. **Laptop OPS fuera de banda:** requiere acceso sudo a la laptop o el túnel SSH; hasta entonces Fase C corre solo-localhost (degradación honesta, no bug).

---

## 11. Lista de tareas ORDENADA (checklist por fase, granularidad PR/commit)

### Fase A — Pipeline (desbloquea fixes)
- [ ] **A-PR1 (cwd):** añadir `WT_SUBDIR` (harness.py:68); `cwd=str(WT_SUBDIR)` en los 5 `subprocess.run` (351,409,440,463,490). *Gate: 0 `File not found` en log.*
- [ ] **A-PR2 (parser):** contrato JSONL en `HUNTER_SYSTEM`; reescribir `_parse_bug_line` (538-596); campos `severity`/`repro` en `Suspect` + `hunter_worker`. *Gate: snippet≠why, sin `::`.*
- [ ] **A-PR3 (fantasmas):** validación de existencia en `yield_suspect` (695-722). *Gate: descarte logueado.*
- [ ] **A-PR4 (salud):** `_HEALTH`+`_mark_health`+`is_healthy`; enchufar en `call_model` (269,296); `health_probe_all`+`health_watchdog` en `main`. *Gate: zombis cuarentenados.*
- [ ] **A-PR5 (gate real):** `_route_for_file`+reescribir `_run_smoke_subset` (1080); detección de patch en `_apply_and_validate` (1042); dedup+lock por archivo en `fixer_worker` (969); umbral severity/quorum (944). *Gate: bug sembrado auto-commiteado.*
- [ ] **A-PR6 (contención):** semáforo `_ollama_sem` en `_call_ollama` (310-338); timeout 360s + max_tokens 2500 en fixer (1002). *Gate: 30B sin timeout en fixer.*
- [ ] **A-PR7 (e2e sweep):** reescribir `e2e_sweep_worker` (1154) al contrato `e2e-runner.js`. *Gate: `e2e_findings>0`.*

### Fase B — VRAM local (crear `fleet.py` parte 1)
- [ ] **B-PR1 (ModelSpec):** extender `tiers.ModelSpec` (46) con 5 campos opcionales.
- [ ] **B-PR2 (discovery):** `fleet.py`: `Gpu/CpuSlot/Node`, `discover_local_gpus`, `discover_models`, `discover_running`, `discover_node`.
- [ ] **B-PR3 (plan):** `est_vram_mib`, `plan_models` (+refinamientos de budget caliente/GPU-vacía/`num_ctx`), `_make_ollama_spec`, `probe_and_quarantine`.
- [ ] **B-PR4 (routing local):** `_call_ollama` lee `base_url`/`num_ctx`/`main_gpu` del spec; integrar `fleet_specs` en carga tier0 (1319), fixer (976), watchdog (1203), log (1283). *Gate: 2060 recibe hunter.*

### Fase C — Multi-host (extender `fleet.py`)
- [ ] **C-PR1 (config):** `build_fleet`, parseo `LOOP_NODES`/`LOOP_NODE_META`, `Fleet`, `node_vram_total_mib` (cascada).
- [ ] **C-PR2 (health):** `probe_node`, `health_loop` (histéresis), `write_fleet_status`→`/tmp/harness-fleet.json`; arrancar hilo en `main`.
- [ ] **C-PR3 (ruteo):** `route`/`pick_gpu`/`nodes_for_role`/`inflight`; invocar router dentro de `call_model` para `ollama:`; reintento acotado + timeout por-nodo.
- [ ] **C-PR4 (2060 como nodo):** instancia Ollama `:11435` pineada a la 2060; registrarla en `LOOP_NODES`.
- [ ] **C-OPS (laptop):** systemd override `OLLAMA_HOST=0.0.0.0:11434` + firewall; verificar desde contenedor; registrar nodo. *Gate: laptop UP en fleet.json.*

### Fase D — TUI (schema + paneles)
- [ ] **D-PR1 (schema v2 harness):** reescribir `write_status` (213); `update_thread_state` (+role/location/node, 186); `_build_roles`/`_build_nodes`/`_probe_node`.
- [ ] **D-PR2 (TUI guard + fix crash):** `read_status` guard (256); eliminar `len()` de tui.py:801-802 (leer `*_n` int); fix label "20/6" en `_show_model_config` (425).
- [ ] **D-PR3 (paneles):** `draw_nodes_panel`+`draw_pipeline_panel`(reescribe 727)+`draw_funnel_panel`; `draw_header` compacto (502); `main` render dashboard (1068-1071). *Gate: TUI sin crash; 2060 visible; contadores etiquetados.*

### Cierre
- [ ] **Z-PR1 (catálogo):** limpiar `tiers.py` (cuarentena estática §8.2; el `ollama:qwen3-coder:30b` de TIER3 como fallback); corregir comentario "10.6GB" (tiers.py:71→~12.3GB).
- [ ] **Z-PR2 (verificación end-to-end):** sembrar bug TS conocido → `fixes_succeeded≥1` auto-commiteado + TUI renderiza el embudo. *Gate maestro de éxito.*

---

**Archivos a crear/tocar (rutas absolutas):**
- Crear: `/workspace/night-harness/fleet.py`; `/workspace/night-harness/docs/superpowers/specs/demeter-v7-master-spec.md` (este documento).
- Editar: `/workspace/night-harness/harness.py` (parser 538, 5 CLIs 351-490, `yield_suspect` 695, `_call_ollama` 310, `_run_smoke_subset` 1080, `_apply_and_validate` 1042, `fixer_worker` 969, `e2e_sweep_worker` 1154, `watchdog` 1203, `update_thread_state` 186, `write_status` 213, `call_model` 262, `main` carga specs ~1319).
- Editar: `/workspace/night-harness/tiers.py` (`ModelSpec` 46, catálogo, comentario 71).
- Editar: `/workspace/night-harness/tui.py` (`read_status` 256, `draw_header` 502, `draw_process_containers` 727, `_show_model_config` 425, `main` 851/~1068).
- Sin cambios estructurales: `orchestrator.py`, `quota.py`, `memory.py`, `e2e-runner.js`.
