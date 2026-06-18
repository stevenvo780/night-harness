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
| **Google** (gemini CLI) | `gemini/` | `gemini-2.5-flash` | tareas **ligeras/medias**, rápido y barato |
| **OpenAI** (codex CLI) | `codex/` | `gpt-5.5` | tareas **difíciles** (razonador fuerte; caro/lento, ~10K tok por respuesta) |

> `opencode/deepseek-v4-pro` NO existe. DeepSeek V4 (pro/flash) vive bajo `opencode-go/`.
> Gemini y Codex usan sus PROPIAS quotas (Google / OpenAI) → más fuentes para repartir carga.
> El ruteo es por **prefijo del modelo**: `gemini/*`→gemini CLI, `codex/*`→codex CLI, resto→opencode.

## Esquema de reparto de carga (tiering por tarea)

| Tier de tarea | Modelo | Suscripción |
|---|---|---|
| **light** (filtros, gen simple, una función) | `minimax/MiniMax-M3` | Minimax |
| **light-fast** (alternativa a Minimax) | `gemini/gemini-2.5-flash` | Google |
| **medium** (codegen/análisis de tamaño medio) | `opencode-go/deepseek-v4-pro` | OpenCode Go |
| **medium-fast** | `opencode-go/deepseek-v4-flash` | OpenCode Go |
| **code** (código complejo) | `opencode-go/kimi-k2.7-code` | OpenCode Go |
| **heavy** (síntesis grande) | `opencode-go/qwen3.7-max` | OpenCode Go |
| **hard** (razonamiento difícil) | `codex/gpt-5.5` | OpenAI (caro) |

**Regla de reparto:** round-robin por **suscripción** (Minimax · OpenCode Go · Google · OpenAI),
no por modelo, para que ninguna se agote. Codex solo para lo realmente difícil (es caro y lento).

## Cómo se usa

- **Tool MCP:** `mcp__cloud-offload__delegar_a_cloud` con `model="opencode-go/deepseek-v4-pro"`.
- **Agente:** `cloud-offloader` (haiku que delega); indícale el `model` en el prompt.
- **Script:** `echo "PROMPT" | scripts/ask-cloud.sh opencode-go/deepseek-v4-pro`.

opencode usa su `~/.local/share/opencode/auth.json` (MiniMax Token Plan + OpenCode Go);
no necesita variables de entorno con llaves.
