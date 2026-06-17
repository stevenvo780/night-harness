#!/usr/bin/env bash
# ask-cloud — delega un PROMPT (por STDIN) a un modelo CLOUD BARATO vía opencode,
# para que Claude Code queme cuota barata (Minimax/OpenCode) en vez de Opus.
# opencode usa su ~/.local/share/opencode/auth.json (MiniMax Token Plan + OpenCode Go);
# NO necesita variables de entorno con llaves.
#
# Uso:   echo "PROMPT COMPLETO" | scripts/ask-cloud.sh [modelo]
#   modelo (opcional, default minimax/MiniMax-M3). Otros: opencode/glm-5,
#   opencode/kimi-k2.6, opencode/gpt-5.5, opencode/gemini-3.5-flash, opencode/minimax-m2.5.
set -uo pipefail
MODEL="${1:-minimax/MiniMax-M3}"
opencode run --model "$MODEL" - 2>/dev/null \
  | sed -E 's/\x1b\[[0-9;]*m//g; /^> build ·/d; /^build ·/d' \
  | sed '/./,$!d'    # quita líneas en blanco iniciales
