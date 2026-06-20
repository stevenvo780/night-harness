# night-harness

Harness de **auto-endurecimiento nocturno** para un proyecto Next.js/TypeScript usando **modelos
LOCALES** (Ollama). Cazadores rápidos buscan bugs por **consenso + refutación**, un resolver los
arregla con **compuerta de typecheck + E2E real (navegador)**, todo **aislado en un git worktree** y
con el **merge a mano**. Pensado para correr toda la noche quemando cómputo local, sin gastar cuota
de un modelo de pago.

> Nace del proyecto **Deméter**. Las rutas/zonas son un ejemplo: adáptalas a tu repo.

## Instalación (Linux)

La TUI se distribuye como **binario self-contained** (sin Python en runtime) +
wrapper bash.

```bash
cd /workspace/night-harness
bash install.sh
```

`install.sh` hace 4 pasos:
1. Instala PyInstaller (si falta) en el user.
2. Compila `tui.py` → `dist/demeter-tui` con `--onefile` (incluye Python + libs).
3. `sudo cp` del binario a `/usr/local/lib/demeter-tui/demeter-tui-bin`.
4. `sudo cp` del wrapper a `/usr/local/bin/demeter-tui`.

Al final ejecuta `demeter-tui check` para validar 5 dependencias. **Solo el binario es
estricto** (falla con exit 1 si falta); las otras 4 son advertencias (`AVISO`) y no
bloquean: binario, `opencode` CLI, status file, pid del harness (corriendo o no),
`ai-usage-live/`. Por eso `install.sh` puede correrlo durante la instalación, cuando
el harness todavía no está activo.

**Uso**:
```bash
demeter-tui              # lanza la TUI (requiere TTY real)
demeter-tui check        # valida deps sin lanzar
demeter-tui status       # muestra el JSON del status
demeter-tui path         # imprime path del binario
demeter-tui version      # versión wrapper + binario
demeter-tui --help       # ayuda
```

**Sin dependencias de Python**: el binario (`/usr/local/lib/demeter-tui/demeter-tui-bin`,
8.3MB) incluye el intérprete Python y todas las libs. `ldd` muestra solo libc/libz/libpthread
(todas del sistema). Cualquier Linux x86_64 con kernel 3.2+ puede ejecutarlo.

**Desinstalar**:
```bash
sudo rm /usr/local/bin/demeter-tui /usr/local/lib/demeter-tui/demeter-tui-bin
sudo rmdir /usr/local/lib/demeter-tui
```

---

## Idea central: los modelos CHICOS rinden con la ESTRUCTURA, no con tamaño

Un modelo chico (p.ej. `qwen3.5:9b`) preguntado a lo abierto "encuentra un bug" alucina mucho — es
mal *juez*. Pero es un buen *generador barato*. El truco es que **la verdad la decida el ACUERDO y la
EJECUCIÓN, no la opinión de un modelo**:

1. **Auto-consistencia (voting):** el hunter corre **3× sobre lo mismo**; una hipótesis solo avanza
   si **recurre (≥2/3)**. Las alucinaciones son aleatorias → no recurren → se filtran solas.
2. **Refutación adversarial:** un 2º pase escéptico intenta *tumbar* la hipótesis ("¿el código ya lo
   maneja?"). Solo sobrevive lo que aguanta.
3. **Triage del resolver:** antes de tocar nada, juzga `REAL` vs `NOT_A_BUG`.
4. **Compuerta de ejecución:** todo fix pasa `tsc` + se re-corre el E2E en navegador (Playwright)
   contra una **DB de test aislada**. Si rompe, se revierte. La verdad es el código corriendo.

En una corrida real esto descartó **decenas de falsos positivos del 9B sin tocar código correcto** y
no introdujo ni un fix erróneo.

---

## Pirámide de costo (v5): cada modelo donde brilla, consumiendo barato primero

Lecciones a fuego: el consenso del MISMO modelo chico NO sube precisión (sus falsos positivos son
**sistemáticos/correlacionados**); y los CLI cloud tienen overhead de arranque → sirven para **JUZGAR**
(baja frecuencia), no para CAZAR a alta frecuencia. La solución: una **escalera de costo** con familias
diversas en capas distintas, **desacoplada por colas**, consumiendo siempre lo más barato primero:

## v7 "Adaptativo" — Cuota-Aware + Memoria Persistente + Cadencia Alta

El v7 (jul-2026, definido en `harness.py:3`) integra el repo
[`ai-usage-live`](https://github.com/stevenvo780/ai-usage-live) para consumir el
monitoreo de cuotas en vivo y planificar qué modelo usar en cada llamada. El
hilo conductor es **minimizar Opus** (caro y escaso) y maximizar Minimax,
Gemini Flash, Sonnet y locales. Toda llamada del harness pasa por
`quota.py`, que consulta `ai-usage.sh daily/weekly` (con cache 60s) y filtra
los modelos cerca del límite antes de cada invocación.

### Tier v7 (modelo → familia → rol → coste relativo 1-10)

| Tier | Modelos (orden) | Rol | Frecuencia |
|---|---|---|---|
| **0 · Escáner local** (2) | `ollama:qwen3-coder:30b` (GPU) + `ollama:qwen3-coder-next:cpu` (CPU) | caza sospechas | altísima, gratis |
| **1 · Filtro** (11) | `minimax/MiniMax-M3` → `minimax/MiniMax-M2.7-highspeed` → `minimax/MiniMax-M2.5-highspeed` → `gemini:gemini-2.5-flash-lite` → `gemini:gemini-3-flash-preview` → `claude:claude-haiku-4-5` + **5 free opencode** (`deepseek-v4-flash-free`, `north-mini-code-free`, `mimo-v2.5-free`, `big-pickle`, `nemotron-3-ultra-free`) | refuta ruido, promoción `suspectq → bugq` | media |
| **2 · Jueces** (8) | `minimax/MiniMax-M3` → `gemini:gemini-2.5-pro` → `gemini:gemini-3-pro-preview` → `codex:gpt-5.5` → `claude:claude-sonnet-4-5` + **3 opencode-go pro** (`kimi-k2.7-code`, `deepseek-v4-pro`, `glm-5.2`) | REAL vs NOT_A_BUG (quorum 2/3) | baja |
| **3 · Fixer** (8) | `codex:gpt-5.5` → `minimax/MiniMax-M3` → `gemini:gemini-2.5-pro` → `claude:claude-sonnet-4-5` → `ollama:qwen3-coder:30b` + **3 opencode-go pro** (`qwen3.7-max`, `deepseek-v4-pro`, `kimi-k2.7-code`) | genera fix, gate `tsc`+E2E, commit | rara |
| **4 · Árbitro** (7) | `ollama:qwen3-coder:30b` → `claude:claude-sonnet-4-5` → `minimax/MiniMax-M3` → `gemini:gemini-2.5-pro` + **3 opencode-go pro** (`qwen3.7-max`, `glm-5.2`, `kimi-k2.7-code`) | veredicto final (SOLO riesgosos) | rarísima |
| **4bis · Opus último recurso** (1) | `claude:claude-opus-4-6` | solo si 4 falla o discordia | extrema |

> Definiciones exactas en `tiers.py:88-566` (`TIER0_HUNTERS`, `TIER1_FILTERS`,
> `TIER2_JUDGES`, `TIER3_FIXERS`, `TIER4_ARBITERS`, `TIER4_OPUS_LAST_RESORT`).
> Cada tier es una tupla inmutable de `ModelSpec`; `quota.py` decide cuál probar
> primero según `ai-usage.sh` en vivo.

#### OpenCode-go provider (jul-2026, 13 modelos)

Pool añadido tras integración de la key opencode-go del usuario. **Sin coste para
el harness**: la key está en `API_KEY` env var y opencode la consume.

- **Free tier (5 modelos, tier 1)**: `opencode:deepseek-v4-flash-free`,
  `opencode:north-mini-code-free`, `opencode:mimo-v2.5-free`, `opencode:big-pickle`,
  `opencode:nemotron-3-ultra-free`. Respuesta 4-24s en test real. Ideales para
  filtros: gratis y rápidos, dan diversidad de familia.
- **Pro tier (8 modelos, tier 2/3/4)**: `opencode-go:kimi-k2.7-code` (Moonshot
  Kimi, especializado en código), `opencode-go:deepseek-v4-pro` (razonador
  fuerte), `opencode-go:glm-5.2` (Z.ai 1M context), `opencode-go:qwen3.7-max`
  (Alibaba top de gama). Calidad/precio competitivas vs Sonnet/Opus.

Total pool: **22 modelos únicos** (2 tier-0 + 11 tier-1 + 8 tier-2 + 8 tier-3 + 7
tier-4 + 1 Opus = 37 membresías de tier, con reutilización: M3 aparece en 4 tiers,
30b y Sonnet en 3 cada uno) — diversidad amplia para consenso, cadencia alta,
coste dominado por local/free.

### Política Opus (v7)

Opus aparece SOLO si se cumplen LAS 3 condiciones:
1. **El fix es riesgoso** (`tiers.is_risky_path`): toca dinero, stock, estado, auth, despacho, factura, caja, permiso, admin.
2. **Los 3 árbitros baratos discordaron** O están todos en cuarentena por cuota.
3. **`is_model_ok('claude:claude-opus-4-6')` retorna True** según el estado de `ai-usage.sh weekly`.

Cuando se invoca, el harness imprime un banner de aviso único por sesión:
```
==============================================================
  ⚠ OPUS ÚLTIMO RECURSO
  El harness detectó que ningún árbitro más barato puede decidir.
  Se invocará claude-opus-4-6 SOLO para fixes riesgosos.
  Coste aprox: 10x Sonnet. Use con moderación.
==============================================================
```

### Control de cuotas (`quota.py`)

Antes de cada llamada a un modelo:
1. Si el estado en cache tiene < 60s, lo reusa. Si no, corre `JSON=1 ai-usage.sh daily` + `ai-usage.sh weekly` (timeout 30s).
2. Parsea los `modelBreakdowns` (efectivos = `input + cacheCreation + output`, **excluye cache reads**).
3. Compara contra `DEFAULT_LIMITS[model]` (sobreescribible por env).
4. Si el modelo está sobre el límite, se omite de la cadena (cae al siguiente).

Las llamadas hechas por el harness se registran en `quota.record()` para reportar
gasto local al final del run (los datos llegan a `ai-usage.sh` con delay; el local
es inmediato).

### Memoria persistente (`memory.py`)

Persiste entre sesiones en `/tmp/loop-memory.json`:
- **Bugs confirmados**: hash estable (file+line+8 palabras) + veces confirmado + fix commit.
- **Falsos positivos**: el 9b los marca como `should_skip` si se han visto 2+ veces.
- **Endurecimiento de zonas**: `clean_rounds >= 3` → la zona se re-escanea con menos prioridad.

API: `Memory.load()`, `MEM.record_bug(...)`, `MEM.record_false_positive(...)`, `MEM.is_hardened(zone)`, `MEM.should_skip(file, line, snippet)`.

### Cadencia alta

- **Multi-escáner**: 4-6 hilos 9b + 2 hilos 30b corren en paralelo (sin contención si `OLLAMA_MAX_LOADED_MODELS>=2`).
- **Reparación especulativa**: cuando 2+ jueces confirman, el fix se lanza SIN esperar a que el siguiente sospechoso se confirme.
- **Hot-reload**: cada fix se valida (`tsc`+E2E) y se re-escanea su zona de inmediato.
- **Converge-and-stop**: `TARGET_CLEAN` rondas sin fix nuevo → código estable, parar.

- **Desacople productor-consumidor:** el escáner local corre a FULL sin esperar a los chiquitos (cola
  `suspectq` de por medio) → el local nunca queda ocioso; los chiquitos consumen a su ritmo.
- **Fallback por cuota:** si un proveedor se agota/rate-limita, va a CUARENTENA (30 min) y la cadena usa
  el siguiente (Codex dura menos → cae a Gemini → Claude → local). **Claude = fallback fiable.**
- **Por qué funciona (datos reales):** familias distintas → errores no correlacionados → su acuerdo es
  señal (validado 2/2 falso-vs-real; el 9B solo no distinguía). El filtro escéptico del chiquito bajó las
  confirmaciones del **43% → ~5%** (sin inundar a los jueces caros). Todo remoto → cero contención de
  memoria con el 9B local. Cuotas separadas → aprovecha Gemini/Codex casi sin tocar Claude.

## Arquitectura (pipeline productor–consumidor)

```
   HUNTERS (qwen3.5:9b, N hilos)  ─┐
   - consenso 3/3 + refutación     ├──►  COLA  ──►  RESOLVER (1 hilo)
   SWEEP (Playwright/chromium) ────┘              - triage REAL vs NOT_A_BUG
   - navega rutas, capta #419/5xx/console          - fix OLD→NEW
                                                    - gate: tsc + re-E2E
                                                    - commit en rama / revert
                                                    OPUS opcional (claude -p) revisa los riesgosos
```

Los hunters (baratos) **llenan la cola sin parar**; el resolver (más lento) **nunca queda ocioso**.
Un módulo se da por "endurecido" tras `TARGET_CLEAN` rondas sin bug nuevo. **Converge-and-stop:** si
una pasada completa no produce ningún fix, el código está estable y para (no re-valida en vano).

---

## ⚠️ Lección de hardware (post-mortem de la 1ª noche) — LÉELA

La 1ª noche el loop "casi no avanzó". El cuello **NO era CPU ni velocidad del modelo**. Medido con
las métricas de Ollama (`load_duration`/`eval_duration`):

| Llamada | wall | carga | generación |
|---|---|---|---|
| 80B-CPU sola | 20s | 0.0s | 19.7s (6 tok/s) ✅ |
| 80B-CPU seguida | 20s | 0.1s | 18.9s ✅ |
| **80B-CPU justo después de una llamada al 9B** | **87s** | 0.1s | 19.3s ← **67s FANTASMA** |

**Causa: contención de memoria entre modelos en Ollama.** Al alternar un modelo grande (~60GB) con
uno chico, las páginas del grande se evictan; repaginarlas en el siguiente request cuesta ~67s (no
cuenta como "carga", es page-fault). Con los hunters (9B) interleaveando entre cada llamada del
resolver (80B), **cada request del resolver esperaba 67s+ → timeout → reintento → más cola → wedge.**

**Mitigaciones (ya en el código):**
- **Por defecto, hunter y resolver usan el MISMO modelo chico** → cero contención.
  (Override por tier: `LOOP_TIER0_HUNTERS`, `LOOP_TIER3_FIXERS` en `tiers.py`.)
- **Sin reintento tras timeout** (reintentar encola otra generación y empeora la cola).
- **Converge-and-stop** (no quemar cómputo re-validando código limpio).

**Si quieres un modelo grande como resolver** (más calidad de fix): garantiza que Ollama mantenga
AMBOS residentes (`OLLAMA_MAX_LOADED_MODELS>=2`, RAM de sobra, `OLLAMA_KEEP_ALIVE` largo), **o** corre
una fase "solo-fix" con el grande sin interleave con el chico. No los alternes request-a-request.

---

## Requisitos

- **Ollama** con un modelo chico de código para el CAZADOR (`qwen3.5:9b`).
- **CLIs de modelos fuertes DIVERSOS** (v3, recomendado): `gemini` (juez/revisor) + `codex` (fixer),
  cada uno en su CAPA → no duplica cuota. Remotos → **cero contención** con el 9B local. `claude` CLI
  también enchufable. Sin ellos, todo cae al local (`LOOP_TIER3_FIXERS=ollama:<modelo>`,
  `LOOP_TIER2_JUDGES=ollama:<modelo>`).
- El **repo objetivo** (Next.js + TypeScript + Drizzle, o adapta) con `npm run typecheck`/`build`.
- **DB de test AISLADA** (Postgres local con el esquema completo) para el dev server del E2E — NUNCA
  apuntes el E2E a producción (muta datos). Playwright + chromium instalados.
- `git` (el harness trabaja en un **worktree** sobre una rama desechable).

## Uso

```bash
# 1) Crea el worktree aislado desde tu HEAD y levanta el dev server contra la DB de test.
git worktree add -B night-harness /tmp/wt HEAD
DATABASE_URL="postgres://localhost:5432/app_e2e" PORT=3011 npm run dev -- --port 3011 &

# 2) Lanza el harness. Monorepo (subdir) + id sembrado para crawl de rutas [id]:
LOOP_WT=/tmp/wt LOOP_SUBDIR=apps/dashboard LOOP_DYNAMIC_ID=<un-id-real-de-tu-db-de-test> \
  LOOP_HOURS=8 LOOP_HUNTERS=3 python3 harness.py

# 3) En la mañana: revisa la rama + /tmp/loop-needs-review.json y MERGEA a mano lo que valga.
```

### Configuración (env vars)

Lista verificada contra `grep os.environ.get harness.py quota.py fleet.py tiers.py`.
Defaults entre paréntesis. Las vars marcadas `(*)` son overrides de tier (lista CSV).

#### Generales
| Var | Default | Qué |
|---|---|---|
| `LOOP_WT` | `/tmp/demeter-loop` | git worktree aislado (rama desechable) |
| `LOOP_SUBDIR` | `apps/dashboard` | subdir del proyecto dentro del worktree |
| `LOOP_HOURS` | `8` | deadline duro (h) |
| `LOOP_TARGET` | `50` | rondas limpias para "endurecer" un módulo |
| `LOOP_LOG` | `/tmp/loop-v6.log` | path del log (mantiene nombre v6 por compat) |
| `LOOP_DEV_URL` | `http://localhost:3011` | dev server para el crawl E2E |
| `LOOP_STORAGE_STATE` | `` | `storageState.json` de Playwright (sesión autenticada); vacío = modo demo |
| `LOOP_DYNAMIC_ID` | `` | id sembrado para crawl de rutas `[id]` (vacío = se omiten) |

#### Concurrencia / hilos
| Var | Default | Qué |
|---|---|---|
| `LOOP_HUNTERS` | `4` | hilos tier-0 locales (hunter pool) |
| `LOOP_SECOND_HILOS` | `2` | hilos second-opinion 30b |
| `LOOP_CLOUD_HUNTERS` | `6` | hilos cloud hunters (tier 0 cloud) |
| `LOOP_FILTER_HILOS` | `6` | hilos tier-1 filtros |
| `LOOP_JUDGE_HILOS` | `3` | hilos tier-2 jueces |
| `LOOP_FIXER_HILOS` | `1` | hilos tier-3 fixer |
| `LOOP_E2E_WORKERS` | `8` | workers E2E en paralelo |
| `LOOP_E2E_ACTIONS` | `24` | interacciones máximas por ruta (forms/botones/modales) |
| `LOOP_E2E_BUDGET_MS` | `70000` | presupuesto de crawl profundo por ruta |

#### Consenso / jueces
| Var | Default | Qué |
|---|---|---|
| `LOOP_CONSENSUS` | `3` | muestras de auto-consistencia del escáner (1 si el cazador es fiable) |
| `LOOP_FILTER_QUORUM` | `2` | votos filtro para ascender `suspectq → bugq` |
| `LOOP_JUDGE_MIN` | `2` | votos REAL para confirmar |
| `LOOP_JUDGE_TOTAL` | `3` | jueces totales que se invocan por sospechoso |

#### Ollama / locales
| Var | Default | Qué |
|---|---|---|
| `LOOP_OLLAMA_BASE` | `http://host.docker.internal:11434` | endpoint de Ollama |
| `LOOP_OLLAMA_NUM_CTX` | `8192` | `num_ctx` enviado a Ollama |
| `LOOP_OLLAMA_GPU_CONC` | `2` | concurrencia por GPU (`parallel` en /api/generate) |
| `LOOP_OLLAMA_KEEP_ALIVE` | `10m` | `keep_alive` por request |
| `LOOP_NODES` | `localhost=<LOOP_OLLAMA_BASE>` | fleet multi-host: `name=url,…` (fleet.py) |

#### Cuotas / cuarentena / health
| Var | Default | Qué |
|---|---|---|
| `LOOP_QUARANTINE_SECS` | `600` | cuarentena (s) tras N fallos seguidos (health) |
| `LOOP_HEALTH_FAIL_THRESHOLD` | `3` | fallos seguidos antes de cuarentenar un modelo |
| `LOOP_HEALTH_PROBE` | `0` | `1` para emitir probes de health adicionales |
| `LOOP_INITIAL_QUARANTINE` | `gemini:gemini-3-flash-preview,gemini:gemini-2.5-flash-lite,minimax/MiniMax-M2.7-highspeed,opencode:mimo-v2.5-free` | seed de cuarentena al arranque (CSV) |

#### Watchdog
| Var | Default | Qué |
|---|---|---|
| `LOOP_STALL_SECS` | `600` | sin avance N s → libera modelos pesados de Ollama |

#### E2E / autofix gate
| Var | Default | Qué |
|---|---|---|
| `LOOP_AUTOFIX_ALL` | `0` | `1` para auto-fijear TODOS los confirmados (no solo high-sev / unanime) |
| `LOOP_GATE_REQUIRE_E2E` | `0` | `1` para que el gate de fixer exija E2E passing (no solo `tsc`) |

#### Overrides de tier `(*)` (CSV de IDs de modelo; orden = preferencia)
| Var | Default (en `tiers.py`) |
|---|---|
| `LOOP_TIER0_HUNTERS` `(*)` | TIER0_HUNTERS (2 modelos: 30b GPU + 30b CPU) |
| `LOOP_TIER0_30B` `(*)` | TIER0_30B_OPINIONS (second opinion) |
| `LOOP_TIER0_CLOUD` `(*)` | TIER0_CLOUD_HUNTERS (8 modelos) |
| `LOOP_TIER1_FILTERS` `(*)` | TIER1_FILTERS (11 modelos) |
| `LOOP_TIER2_JUDGES` `(*)` | TIER2_JUDGES (8 modelos) |
| `LOOP_TIER3_FIXERS` `(*)` | TIER3_FIXERS (8 modelos) |
| `LOOP_TIER4_ARBITERS` `(*)` | TIER4_ARBITERS (7 modelos) |

#### Otras
| Var | Default | Qué |
|---|---|---|
| `AI_USAGE_DIR` | `/workspace/ai-usage-live` | path del repo `ai-usage-live` (lo lee `quota.py` para cache de cuotas) |
| `MINIMAX_API_KEY` | `` | API key de Minimax (si no está en `~/.local/share/opencode/auth.json`) |
| `MINIMAX_API_BASE` | `https://api.minimax.io/v1` | base URL de la API Minimax |

Las **zonas de código** que cazan los hunters se descubren dinámicamente con
`discover_code_zones()` (harness.py:2040): agrupa los `.ts`/`.tsx` bajo `app/(panel)/`
y `lib/` por prefijo (límite 30 archivos por zona). No hay `MODULES` que editar a
mano. Las **rutas E2E** también se descubren solas del directorio `app/`
(App Router) — no hay que listarlas.

## Seguridad

- Trabaja en un **git worktree** sobre rama desechable → **nunca toca tu working tree ni producción**.
- Cada fix pasa **typecheck + re-E2E**; si rompe, **se revierte** (nunca empeora).
- Los fixes que tocan dinero/stock/estado/auth se marcan en `/tmp/loop-needs-review.json`.
- **El merge a tu rama principal lo haces tú, a mano.** El harness solo commitea en la rama aislada.

## Archivos

- `harness.py` — orquestador (hunters de código + crawl E2E + resolver + watchdog + opus + memoria + converge-stop).
- `e2e-runner.js` — crawler PROFUNDO Playwright: navega + llena forms + submit + clickea botones + abre
  modales + cambia selects; capta consola/#419/HTTP≥400/requests fallidas/UI de error.
- Estado en runtime: `/tmp/loop-nocturno.log`, `/tmp/loop-nocturno-status.json`, `/tmp/loop-memory.json`,
  `/tmp/loop-needs-review.json`, `/tmp/loop-e2e-findings.json` (TODOS los hallazgos E2E, para revisión).

## Estado (v2 — intensivo + resiliente)

- **Cobertura real:** descubre TODAS las rutas del App Router (en Deméter: 55) y **ejercita cada una a
  fondo** (forms, submit, botones, modales) — ya no solo navega. Cada hallazgo se registra para revisión
  humana aunque no se auto-arregle.
- **Resiliencia:** `wait_for_model` con backoff (si usas un modelo grande, **espera a que arranque/se
  libere** antes de cada fix); **watchdog** que detecta stalls y **libera modelos pesados que acaparen
  Ollama** (recuperación automática del wedge nocturno); sin reintentos que encolen.
- **Lecciones de la 1ª noche aplicadas:** 1 modelo por defecto (sin contención), sin reintento,
  converge-and-stop.

Próximo: seeding automático de datos + `storageState` para flujos de MUTACIÓN aún más profundos
(tomar→aceptar→despachar→facturar end-to-end con datos reales en la DB de test).
