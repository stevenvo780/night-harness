# Modelos cloud para offload de Claude Code (reparto entre 2 suscripciones)

Claude Code delega trabajo a modelos cloud baratos vía `opencode` (tool MCP
`mcp__cloud-offload__delegar_a_cloud`, agente `cloud-offloader`, o `scripts/ask-cloud.sh`),
para **no quemar cuota de Opus/Claude**. Objetivo: **repartir carga entre las DOS
suscripciones** del usuario y no agotar una sola.

## Suscripciones y modelos verificados (jun-2026, responden OK)

| Suscripción | Provider (prefijo) | Modelos | Uso sugerido |
|---|---|---|---|
| **Minimax** (MiniMax Token Plan) | `minimax/` | `minimax/MiniMax-M3` | tareas **ligeras/rápidas**: filtros, gen simple, reviews |
| **OpenCode Go** | `opencode-go/` | `deepseek-v4-flash` | tareas **medias-rápidas** |
| | | `deepseek-v4-pro` | tareas **medias** (codegen/análisis) |
| | | `kimi-k2.7-code` | **código** especializado |
| | | `glm-5.2`, `qwen3.7-max` | tareas **pesadas** |
| OpenCode (free, bonus) | `opencode/` | `deepseek-v4-flash-free`, `mimo-v2.5-free` | relleno gratis (rate-limited) |

> `opencode/deepseek-v4-pro` NO existe. DeepSeek V4 (pro/flash) vive bajo `opencode-go/`.

## Esquema de reparto de carga (tiering por tarea)

| Tier de tarea | Modelo | Suscripción |
|---|---|---|
| **light** (filtros, gen simple, una función) | `minimax/MiniMax-M3` | Minimax |
| **medium** (codegen/análisis de tamaño medio) | `opencode-go/deepseek-v4-pro` | OpenCode Go |
| **medium-fast** | `opencode-go/deepseek-v4-flash` | OpenCode Go |
| **code** (código complejo) | `opencode-go/kimi-k2.7-code` | OpenCode Go |
| **heavy** (síntesis grande) | `opencode-go/qwen3.7-max` | OpenCode Go |

**Regla de reparto:** alternar entre `minimax/*` y `opencode-go/*` a lo largo de un
workflow para que ninguna suscripción se agote (round-robin por suscripción, no por modelo).

## Cómo se usa

- **Tool MCP:** `mcp__cloud-offload__delegar_a_cloud` con `model="opencode-go/deepseek-v4-pro"`.
- **Agente:** `cloud-offloader` (haiku que delega); indícale el `model` en el prompt.
- **Script:** `echo "PROMPT" | scripts/ask-cloud.sh opencode-go/deepseek-v4-pro`.

opencode usa su `~/.local/share/opencode/auth.json` (MiniMax Token Plan + OpenCode Go);
no necesita variables de entorno con llaves.
