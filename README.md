# night-harness

Harness de **auto-endurecimiento nocturno** para un proyecto Next.js/TypeScript usando **modelos
LOCALES** (Ollama). Cazadores rápidos buscan bugs por **consenso + refutación**, un resolver los
arregla con **compuerta de typecheck + E2E real (navegador)**, todo **aislado en un git worktree** y
con el **merge a mano**. Pensado para correr toda la noche quemando cómputo local, sin gastar cuota
de un modelo de pago.

> Nace del proyecto **Deméter**. La config de `MODULES`/rutas es un ejemplo: adáptala a tu repo.

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

| Tier | Modelo (def.) | Frecuencia | Rol |
|---|---|---|---|
| **0 · Escáner** | **9B LOCAL** (N hilos, HTTP) | altísima (gratis) | escanea TODO a full (consenso) → marca ventanas sospechosas → `suspectq` |
| **1 · Filtro** | chiquitos: **Haiku + Gemini-Flash** | media (solo sospechosas) | filtran ESCÉPTICAMENTE el ruido del local; lo sólido → `bugq` |
| **2 · Jueces** | **Gemini-3-Pro + Codex + Sonnet + 30b-local** | baja (solo confirmado) | REAL vs NOT_A_BUG; escalera barato→caro que corta al confirmar o al ser imposible |
| **3 · Fixer** | **Codex** → Gemini-Pro → Claude → local | rara (bugs reales) | genera el fix → gate `tsc`+E2E → commit |
| **4 · Árbitro** | 🏆 **Opus** → Gemini-Pro → Sonnet | rarísima (riesgoso+contestado) | veredicto final |

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
- **Por defecto, hunter y resolver usan el MISMO modelo chico** → cero contención. (`LOOP_RESOLVER`)
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
  también enchufable. Sin ellos, todo cae al local (`LOOP_FIXER=ollama:<modelo>`, `LOOP_JUDGES=ollama:<modelo>`).
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

| Var | Default | Qué |
|---|---|---|
| `LOOP_BASE` | `http://host.docker.internal:11434` | endpoint de Ollama |
| `LOOP_HUNTER` | `qwen3.5:9b` | **escáner LOCAL** (tier 0, HTTP rápido/gratis) |
| `LOOP_SCANNERS` | `4` | hilos del escáner local (corre a FULL) |
| `LOOP_CONSENSUS` | `3` | muestras de auto-consistencia del escáner (1 si el cazador es fiable) |
| **`LOOP_CHIQUITOS`** | `claude:haiku,gemini:gemini-2.5-flash` | **filtro/caza** (tier 1): filtran las sospechosas |
| `LOOP_CHIQUITO_HILOS` | `2` | hilos chiquitos (consumen `suspectq`) |
| `LOOP_SUSPECT_MAX` | `50` | runway del local (cola de sospechosas) |
| **`LOOP_JUDGES`** | `gemini-3-pro, codex, sonnet, 30b-local` | **jueces fuertes** (tier 2); orden = escalera barato→caro |
| `LOOP_JUDGE_MIN` | `2` | votos REAL para confirmar |
| **`LOOP_FIXER`** | `codex, gemini-3-pro, sonnet, 30b-local` | **cadena fixer** (tier 3) con fallback |
| `LOOP_REVIEWER` | `gemini-3-pro, sonnet` | revisor de fixes riesgosos (cadena) |
| **`LOOP_ARBITER`** | `opus, gemini-3-pro, sonnet` | 🏆 **árbitro** (tier 4), cadena con fallback |
| `LOOP_QUOTA_COOLDOWN` | `1800` | cuarentena (s) de un proveedor agotado antes de reintentar |
| `LOOP_TARGET` | `50` | rondas limpias para "endurecer" un módulo |
| `LOOP_HOURS` | `8` | deadline duro |
| `LOOP_DEV_URL` | `http://localhost:3011` | dev server para el crawl E2E |
| `LOOP_WT` | `/tmp/wt` | git worktree aislado (rama desechable) |
| `LOOP_SUBDIR` | `` (raíz) | subdir del proyecto en el worktree, p.ej. `apps/dashboard` |
| `LOOP_STORAGE_STATE` | `` | `storageState.json` de Playwright (sesión autenticada); vacío = modo demo |
| `LOOP_DYNAMIC_ID` | `` | id sembrado para crawl de rutas `[id]` (vacío = se omiten) |
| `LOOP_BIG_RESOLVER` | `0` | `1` si el resolver es grande → espera disponibilidad con backoff antes de cada fix |
| `LOOP_E2E_ACTIONS` | `24` | interacciones máximas por ruta (forms/botones/modales) |
| `LOOP_E2E_BUDGET_MS` | `70000` | presupuesto de crawl profundo por ruta |
| `LOOP_STALL_SECS` | `360` | watchdog: sin avance N s → recuperación (libera modelos pesados de Ollama) |

`MODULES` (para el cazador de CÓDIGO) se edita en `harness.py`. Las **rutas E2E se descubren solas**
del directorio `app/` (App Router) — no hay que listarlas.

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
