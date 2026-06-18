#!/usr/bin/env bash
# ask-cloud — delega un PROMPT (por STDIN) a un modelo CLOUD BARATO, repartiendo
# carga entre TUS suscripciones/quotas para no agotar una sola. Para que Claude Code
# queme cuota barata en vez de Opus. Rutea por prefijo del modelo:
#
#   opencode/*, opencode-go/*, minimax/*  ->  opencode run   (Minimax Token Plan / OpenCode Go)
#   gemini/*                              ->  gemini -m       (quota Google)
#   codex/*                               ->  codex exec      (quota OpenAI; caro, para lo dificil)
#
# Uso:   echo "PROMPT" | scripts/ask-cloud.sh [modelo]
#   default: minimax/MiniMax-M3
#   ej: gemini/gemini-2.5-flash · codex/gpt-5.5 · opencode-go/deepseek-v4-pro
set -euo pipefail
MODEL="${1:-minimax/MiniMax-M3}"
PROMPT="$(cat)"
STRIP_ANSI='s/\x1b\[[0-9;]*m//g'

case "$MODEL" in
  gemini/*)
    gemini -m "${MODEL#gemini/}" -p "$PROMPT" --output-format text </dev/null 2>/dev/null \
      | sed -E "$STRIP_ANSI" | grep -vE 'Ripgrep is not available|Falling back to' ;;
  codex/*)
    codex exec --model "${MODEL#codex/}" --skip-git-repo-check "$PROMPT" 2>/dev/null \
      | sed -E "$STRIP_ANSI" \
      | grep -vE '^(warning:|tokens used|[0-9,]+$|codex$|-{3,})' ;;
  *)  # opencode / opencode-go / minimax  -> opencode CLI (lee stdin con '-')
    printf '%s' "$PROMPT" | opencode run --model "$MODEL" - 2>/dev/null \
      | sed -E "$STRIP_ANSI; /^> build ·/d; /^build ·/d" | sed '/./,$!d' ;;
esac
