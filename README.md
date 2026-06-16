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

- **Ollama** accesible (local o por túnel) con al menos un modelo de código (`qwen3.5:9b` va de
  sobra para hunter+resolver). Opcional `claude` CLI para la revisión Opus.
- El **repo objetivo** (Next.js + TypeScript + Drizzle, o adapta) con `npm run typecheck`/`build`.
- **DB de test AISLADA** (Postgres local con el esquema completo) para el dev server del E2E — NUNCA
  apuntes el E2E a producción (muta datos). Playwright + chromium instalados.
- `git` (el harness trabaja en un **worktree** sobre una rama desechable).

## Uso

```bash
# 1) Crea el worktree aislado desde tu HEAD y levanta el dev server contra la DB de test.
git worktree add -B night-harness /tmp/wt HEAD
DATABASE_URL="postgres://localhost:5432/app_e2e" PORT=3011 npm run dev -- --port 3011 &

# 2) Lanza el harness (todo configurable por env; ver abajo).
LOOP_HOURS=8 LOOP_HUNTERS=3 python3 harness.py

# 3) En la mañana: revisa la rama + /tmp/loop-needs-review.json y MERGEA a mano lo que valga.
```

### Configuración (env vars)

| Var | Default | Qué |
|---|---|---|
| `LOOP_BASE` | `http://host.docker.internal:11434` | endpoint de Ollama |
| `LOOP_HUNTER` | `qwen3.5:9b` | modelo cazador (rápido) |
| `LOOP_RESOLVER` | `qwen3.5:9b` | modelo que arregla (ver lección de contención) |
| `LOOP_HUNTERS` | `4` | hilos cazadores |
| `LOOP_TARGET` | `50` | rondas limpias para "endurecer" un módulo |
| `LOOP_HOURS` | `8` | deadline duro |
| `LOOP_DEV_URL` | `http://localhost:3011` | dev server para el sweep E2E |

`MODULES` (mapa módulo → archivos + rutas) se edita arriba en `harness.py`.

## Seguridad

- Trabaja en un **git worktree** sobre rama desechable → **nunca toca tu working tree ni producción**.
- Cada fix pasa **typecheck + re-E2E**; si rompe, **se revierte** (nunca empeora).
- Los fixes que tocan dinero/stock/estado/auth se marcan en `/tmp/loop-needs-review.json`.
- **El merge a tu rama principal lo haces tú, a mano.** El harness solo commitea en la rama aislada.

## Archivos

- `harness.py` — orquestador (hunters + sweep + resolver + opus + memoria + converge-stop).
- `e2e-runner.js` — sonda Playwright (navega una ruta, capta errores de consola/#419/5xx).
- Estado en runtime: `/tmp/loop-nocturno.log`, `/tmp/loop-nocturno-status.json`,
  `/tmp/loop-memory.json`, `/tmp/loop-needs-review.json`.

## Estado

Funcional. Validó un código real (descartó decenas de falsos positivos sin dañar nada). Las mejoras
del post-mortem (1 modelo, sin reintento, converge-stop) están aplicadas. Próximos pasos posibles:
watchdog de stall, fase "solo-fix" con modelo grande, seeding de datos para E2E de mutación.
