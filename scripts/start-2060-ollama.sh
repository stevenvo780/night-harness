#!/bin/bash
# start-2060-ollama.sh — inicia una 2ª instancia Ollama pineada a RTX 2060 (:11435)
# Uso: bash scripts/start-2060-ollama.sh
# Idempotente: si ya corre, exit 0

set -e

# Obtener UUID de la 2060 (GPU1) via nvidia-smi
GPU1_UUID=$(nvidia-smi --query-gpu=uuid --id=1 --format=csv,noheader,nounits 2>/dev/null || true)
if [[ -z "$GPU1_UUID" ]]; then
    echo "[!] No GPU1 detectada. Abortando."
    exit 1
fi

# Verificar si ya hay un servidor en :11435
if nc -z 127.0.0.1 11435 2>/dev/null; then
    echo "[✓] Ollama ya corre en :11435. Nada que hacer."
    exit 0
fi

# Log file
LOGFILE="/tmp/ollama-2060.log"

# Comando: pineado a GPU1, puerto 11435, max 1 modelo cargado
export CUDA_VISIBLE_DEVICES="$GPU1_UUID"
export OLLAMA_HOST="127.0.0.1:11435"
export OLLAMA_MAX_LOADED_MODELS=1
export OLLAMA_KEEP_ALIVE="10m"

echo "[→] Iniciando Ollama en :11435 pineado a GPU1 ($GPU1_UUID)..."
nohup ollama serve > "$LOGFILE" 2>&1 &
OLLAMA_PID=$!
echo "[✓] Ollama PID=$OLLAMA_PID. Log: $LOGFILE"

# Esperar a que despierte
for i in {1..30}; do
    if nc -z 127.0.0.1 11435 2>/dev/null; then
        echo "[✓] :11435 responde. Listo."
        exit 0
    fi
    sleep 1
done

echo "[!] :11435 no respondió en 30s. Verificar $LOGFILE."
exit 1
