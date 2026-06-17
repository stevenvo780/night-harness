#!/usr/bin/env bash
# install.sh — compila e instala demeter-tui (wrapper + binario)
# Uso: bash install.sh
#
# Pasos:
#   1. Instala PyInstaller en el user si no está
#   2. Compila tui.py → dist/demeter-tui (--onefile, self-contained)
#   3. sudo cp binario a /usr/local/lib/demeter-tui/demeter-tui-bin
#   4. sudo cp wrapper a /usr/local/bin/demeter-tui
#   5. Ejecuta `demeter-tui check` para validar
set -euo pipefail

HARNESS_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$HARNESS_DIR"

echo "=== demeter-tui installer ==="
echo "harness dir: $HARNESS_DIR"
echo

# --- 1. PyInstaller ---
echo "[1/4] verificando PyInstaller..."
if ! python3 -c "import PyInstaller" 2>/dev/null; then
    echo "      instalando PyInstaller (--user --break-system-packages)..."
    pip3 install --user --break-system-packages pyinstaller 2>&1 | tail -3
else
    echo "      OK: $(python3 -c 'import PyInstaller; print(PyInstaller.__version__)')"
fi
echo

# --- 2. Compilar ---
echo "[2/4] compilando tui.py → dist/demeter-tui..."
rm -rf build dist
python3 -m PyInstaller \
    --onefile \
    --name demeter-tui \
    --console \
    --noconfirm \
    --clean \
    tui.py 2>&1 | tail -3
if [ ! -x dist/demeter-tui ]; then
    echo "ERROR: compilación falló — no se generó dist/demeter-tui" >&2
    exit 1
fi
BIN_SIZE=$(stat -c %s dist/demeter-tui)
echo "      OK: $BIN_SIZE bytes"
echo

# --- 3. Instalar binario ---
echo "[3/4] instalando binario a /usr/local/lib/demeter-tui/..."
sudo mkdir -p /usr/local/lib/demeter-tui
sudo cp dist/demeter-tui /usr/local/lib/demeter-tui/demeter-tui-bin
sudo chmod 755 /usr/local/lib/demeter-tui/demeter-tui-bin
echo "      OK: /usr/local/lib/demeter-tui/demeter-tui-bin"
echo

# --- 4. Instalar wrapper + validar ---
echo "[4/4] instalando wrapper a /usr/local/bin/demeter-tui..."
sudo cp bin/demeter-tui /usr/local/bin/demeter-tui
sudo chmod 755 /usr/local/bin/demeter-tui
echo "      OK: /usr/local/bin/demeter-tui"
echo

echo "=== Validación final ==="
demeter-tui check
