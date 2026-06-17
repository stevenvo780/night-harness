#!/usr/bin/env python3
"""
tui.py — TUI interactiva del harness v6 (curses).

Paneles:
  1. Header: estado del harness (PID, uptime, ronda, contadores).
  2. Cadencia por modelo (calls/seg, barra horizontal, color por familia).
  3. CPU / GPU en vivo (nproc, load, nvidia-smi).
  4. Ollama: modelos cargados en VRAM.
  5. Log: últimas N líneas con código de color.
  6. Footer: teclas disponibles.

Teclas:
  q / Q    salir
  r / R    forzar refresh inmediato
  p / P    pausar/reanudar auto-refresh
  c / C    limpiar el log mostrado
  l / L    cambiar layout (compact/full/metrics-only)
  s / S    lanzar / detener ciclo del harness
  d / D    cambiar duración del ciclo (1h/2h/4h/8h/indefinido)
  ?        ayuda
  ↑/↓      scroll en el panel de log

USO:
  ./tui.py [STATUS_FILE] [LOG_FILE]
    defaults: /tmp/loop-v6-status.json, /tmp/loop-v6.log
"""
from __future__ import annotations

import curses
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, deque
from datetime import datetime

STATUS_FILE = sys.argv[1] if len(sys.argv) > 1 else '/tmp/loop-v6-status.json'
LOG_FILE = sys.argv[2] if len(sys.argv) > 2 else '/tmp/loop-v6.log'
HARNESS_PID_FILE = '/tmp/loop-v6.pid'
OLLAMA_URL = 'http://host.docker.internal:11434'

REFRESH_SEC = 2.0
LOG_LINES = 200  # cuántas líneas del log mantener en memoria

# Colores.
COLOR_PAIR_DEFAULT = 1
COLOR_PAIR_HEADER = 2
COLOR_PAIR_GREEN = 3
COLOR_PAIR_YELLOW = 4
COLOR_PAIR_RED = 5
COLOR_PAIR_CYAN = 6
COLOR_PAIR_MAGENTA = 7
COLOR_PAIR_BLUE = 8
COLOR_PAIR_GRAY = 9
COLOR_PAIR_LOCAL = 10  # cyan brillante para modelos locales
COLOR_PAIR_CLOUD = 11  # magenta para cloud
COLOR_PAIR_BAR = 12
COLOR_PAIR_BAR_FULL = 13  # barra "llena" cuando hay cadencia alta
COLOR_PAIR_HIGHLIGHT = 14


def init_colors() -> bool:
    """Inicializa los pares de color. Retorna False si no se pudo (terminal
    sin soporte de color). Funciona con terminales de 8, 16, o 256 colores."""
    try:
        curses.start_color()
        curses.use_default_colors()
    except (curses.error, Exception):
        return False

    # Detectar cuántos colores soporta la terminal.
    # Típicos: 8 (básica), 16, 88, 256 (xterm-256color).
    n_colors = curses.COLORS
    n_pairs = curses.COLOR_PAIRS

    # Si la terminal tiene menos de 16 colores, limitamos a los 8 básicos
    # y evitamos grises/colores extendidos.
    limited = n_colors < 16

    # Helper: en lugar de pasar el número de color "gris" (que no existe en
    # terminales de 8 colores), usamos WHITE con A_DIM. Esto lo aplicamos
    # en draw_*_panel cuando queremos "gris".
    # Aquí simplemente NO creamos el par GRAY si la terminal es limited;
    # los callers usan curses.A_DIM directamente.

    pairs = [
        # (pair_id, fg, bg, atributos_extra)
        (COLOR_PAIR_DEFAULT, -1, -1, 0),
        (COLOR_PAIR_HEADER, curses.COLOR_CYAN, -1, 0),
        (COLOR_PAIR_GREEN, curses.COLOR_GREEN, -1, 0),
        (COLOR_PAIR_YELLOW, curses.COLOR_YELLOW, -1, 0),
        (COLOR_PAIR_RED, curses.COLOR_RED, -1, 0),
        (COLOR_PAIR_CYAN, curses.COLOR_CYAN, -1, 0),
        (COLOR_PAIR_MAGENTA, curses.COLOR_MAGENTA, -1, 0),
        (COLOR_PAIR_BLUE, curses.COLOR_BLUE, -1, 0),
        (COLOR_PAIR_LOCAL, curses.COLOR_CYAN, -1, 0),
        (COLOR_PAIR_CLOUD, curses.COLOR_MAGENTA, -1, 0),
        (COLOR_PAIR_BAR_FULL, curses.COLOR_GREEN, -1, 0),
    ]

    # Pares que usan color "gris" (8). Solo si la terminal soporta >=16 colores.
    if not limited:
        try:
            # COLOR_GRAY real existe en ncurses 1.9.9+ (no en 8-color).
            # Si no, usamos un fallback: dim white.
            if hasattr(curses, 'COLOR_GRAY') and curses.COLOR_GRAY < n_colors:
                pairs.append((COLOR_PAIR_GRAY, curses.COLOR_GRAY, -1, 0))
            else:
                pairs.append((COLOR_PAIR_GRAY, curses.COLOR_WHITE, -1, curses.A_DIM))
        except (curses.error, ValueError, AttributeError):
            pass

    # Bar vacía: usa dim white en lugar de gris.
    pairs.append((COLOR_PAIR_BAR, curses.COLOR_WHITE, -1, curses.A_DIM))

    # Highlight: solo si la terminal tiene COLOR_BLACK.
    if curses.COLOR_BLACK < n_colors:
        pairs.append((COLOR_PAIR_HIGHLIGHT, curses.COLOR_BLACK, curses.COLOR_CYAN, 0))

    for pair, fg, bg, attrs in pairs:
        if pair >= n_pairs:
            continue  # terminal no soporta tantos pares
        try:
            curses.init_pair(pair, fg, bg)
        except (curses.error, ValueError):
            pass  # Color no soportado, seguimos.

    return True


class LogBuffer:
    """Buffer circular de las últimas N líneas del log, con timestamps."""

    def __init__(self, max_lines: int = LOG_LINES):
        self.max = max_lines
        self.lines: deque[tuple[float, str]] = deque(maxlen=max_lines)
        self._cursor = 0  # posición en el buffer para scroll
        self._last_size = 0

    def update(self, path: str) -> bool:
        """Lee el archivo y agrega las líneas nuevas. Retorna True si hubo cambios."""
        try:
            with open(path) as f:
                # tail -n con seek eficiente.
                f.seek(0, 2)
                size = f.tell()
                if size < self._last_size:
                    # Archivo rotado, releer todo.
                    self._last_size = 0
                    self.lines.clear()
                if size > self._last_size:
                    f.seek(self._last_size)
                    new = f.read()
                    self._last_size = size
                    added = 0
                    for line in new.splitlines():
                        # Parsear timestamp HH:MM:SS.
                        m = re.match(r'\[(\d{2}):(\d{2}):(\d{2})\]', line)
                        if m:
                            now = time.localtime()
                            try:
                                ts = time.mktime((
                                    now.tm_year, now.tm_mon, now.tm_mday,
                                    int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                    0, 0, 0,
                                ))
                            except (ValueError, OverflowError):
                                ts = time.time()
                        else:
                            ts = time.time()
                        self.lines.append((ts, line))
                        added += 1
                    return added > 0
        except (OSError, FileNotFoundError):
            return False
        return False

    def scroll_up(self, n: int = 1):
        self._cursor = min(self._cursor + n, max(0, len(self.lines) - 1))

    def scroll_down(self, n: int = 1):
        self._cursor = max(0, self._cursor - n)

    def reset_cursor(self):
        self._cursor = 0


def model_color(model_id: str) -> int:
    """Asigna un color según la familia del modelo."""
    if model_id.startswith('ollama:'):
        return curses.color_pair(COLOR_PAIR_LOCAL)
    if model_id.startswith('minimax/'):
        return curses.color_pair(COLOR_PAIR_LOCAL)
    if model_id.startswith('gemini:'):
        return curses.color_pair(COLOR_PAIR_CLOUD)
    if model_id.startswith('claude:'):
        return curses.color_pair(COLOR_PAIR_MAGENTA)
    if model_id.startswith('codex:'):
        return curses.color_pair(COLOR_PAIR_CLOUD)
    return curses.color_pair(COLOR_PAIR_DEFAULT)


def gray_attr() -> int:
    """Atributo para texto 'gris' (dim) que funciona en cualquier terminal."""
    return curses.A_DIM


def model_family_short(model_id: str) -> str:
    """Etiqueta corta para mostrar en la barra de cadencia."""
    if model_id.startswith('ollama:qwen3.5:9b'):
        return '9b'
    if model_id.startswith('ollama:qwen3-coder:30b'):
        return '30b'
    if model_id.startswith('ollama:qwen3-coder-next:cpu'):
        return '80b-cpu'
    if model_id.startswith('minimax/MiniMax-M3'):
        return 'mm-m3'
    if model_id.startswith('minimax/MiniMax-M2.5-highspeed'):
        return 'mm-m2.5hs'
    if model_id.startswith('minimax/MiniMax-M2.7-highspeed'):
        return 'mm-m2.7hs'
    if model_id.startswith('gemini:gemini-2.5-flash-lite'):
        return 'gem-fl'
    if model_id.startswith('gemini:gemini-2.5-pro'):
        return 'gem-pro'
    if model_id.startswith('gemini:gemini-3-flash-preview'):
        return 'gem-3fl'
    if model_id.startswith('gemini:gemini-3-pro-preview'):
        return 'gem-3pro'
    if model_id.startswith('claude:claude-sonnet-4-5'):
        return 'sonnet'
    if model_id.startswith('claude:claude-haiku-4-5'):
        return 'haiku'
    if model_id.startswith('codex:gpt-5.5'):
        return 'codex'
    if model_id.startswith('claude:claude-opus-4-6'):
        return 'opus'
    # Truncar el prefijo.
    return model_id.split(':')[-1].split('/')[-1][:14]


def read_status(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, FileNotFoundError):
        return {}


def read_log_calls(path: str, window_sec: int = 30) -> Counter:
    """Cuenta las llamadas por modelo en los últimos N segundos del log."""
    cutoff = time.time() - window_sec
    counts: Counter = Counter()
    try:
        with open(path) as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200_000), 0)  # últimas ~200KB
            for line in f:
                m = re.match(r'\[(\d{2}):(\d{2}):(\d{2})\].*\[call\] (\S+) ', line)
                if not m:
                    continue
                now = time.localtime()
                try:
                    ts = time.mktime((
                        now.tm_year, now.tm_mon, now.tm_mday,
                        int(m.group(1)), int(m.group(2)), int(m.group(3)), 0, 0, 0,
                    ))
                except (ValueError, OverflowError):
                    continue
                if ts < cutoff:
                    continue
                counts[m.group(4)] += 1
    except (OSError, FileNotFoundError):
        pass
    return counts


def get_cpu_stats() -> dict:
    try:
        out = subprocess.run(['uptime'], capture_output=True, text=True, timeout=2).stdout
        load = out.split('load average:')[1].strip() if 'load average:' in out else '?'
    except Exception:
        load = '?'
    try:
        cpu_line = subprocess.run(['top', '-bn1'], capture_output=True, text=True, timeout=2).stdout
        idle = '?'
        for line in cpu_line.splitlines():
            if line.startswith('%Cpu'):
                m = re.search(r'(\d+\.\d+)\s*id', line)
                if m:
                    idle = m.group(1)
                    break
    except Exception:
        idle = '?'
    return {'load': load, 'idle_pct': idle}


def get_gpu_stats() -> list[dict]:
    out = []
    try:
        result = subprocess.run(
            ['nvidia-smi',
             '--query-gpu=index,name,memory.used,memory.total,utilization.gpu',
             '--format=csv,noheader'],
            capture_output=True, text=True, timeout=2,
        )
        for line in result.stdout.splitlines():
            parts = [p.strip() for p in line.split(', ')]
            if len(parts) >= 5:
                out.append({
                    'index': parts[0],
                    'name': parts[1][:24],
                    'mem_used_mib': int(parts[2].replace('MiB', '').strip()),
                    'mem_total_mib': int(parts[3].replace('MiB', '').strip()),
                    'util_pct': int(parts[4].replace('%', '').strip()),
                })
    except Exception:
        pass
    return out


def get_ollama_models() -> list[dict]:
    out = []
    try:
        import urllib.request
        with urllib.request.urlopen(f'{OLLAMA_URL}/api/ps', timeout=2) as r:
            d = json.loads(r.read())
        for m in d.get('models', []):
            out.append({
                'name': m['name'],
                'vram_gb': m.get('size_vram', 0) / 1e9,
            })
    except Exception:
        pass
    return out


def check_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def draw_header(stdscr, status: dict, harness_alive: bool, y: int = 0) -> int:
    """Header con el estado del harness. Retorna la siguiente línea libre."""
    h, w = stdscr.getmaxyx()
    if harness_alive:
        title = f"HARNESS v6 — ACTIVO (PID {status.get('pid', '?')})"
        title_color = curses.color_pair(COLOR_PAIR_GREEN) | curses.A_BOLD
    else:
        title = f"HARNESS v6 — MUERTO (PID {status.get('pid', '?')})"
        title_color = curses.color_pair(COLOR_PAIR_RED) | curses.A_BOLD

    stdscr.addstr(y, 1, '═' * (w - 2), curses.color_pair(COLOR_PAIR_HEADER))
    stdscr.addstr(y, max(1, (w - len(title)) // 2), title, title_color)
    y += 1
    s = status.get('stats', {})
    up = status.get('uptime_sec', 0)
    h_, m_, s_ = up // 3600, (up % 3600) // 60, up % 60
    uptime_str = f"{h_:02d}:{m_:02d}:{s_:02d}"
    line1 = (
        f"  Uptime: {uptime_str}  │  Rounds: {s.get('rounds', 0)}  "
        f"│  Clean: {s.get('clean_rounds_in_a_row', 0)}  "
        f"│  Suspects: {s.get('suspects_produced', 0)}  "
        f"│  Refutados: {s.get('suspects_refuted_by_filter', 0)}"
    )
    stdscr.addstr(y, 1, line1[:w - 2], curses.color_pair(COLOR_PAIR_DEFAULT))
    y += 1
    bugs = s.get('bugs_confirmed', 0)
    bug_color = COLOR_PAIR_RED if bugs > 0 else COLOR_PAIR_DEFAULT
    fixes = s.get('fixes_succeeded', 0)
    fix_color = COLOR_PAIR_GREEN if fixes > 0 else COLOR_PAIR_DEFAULT
    opus = s.get('opus_invocations', 0)
    opus_color = COLOR_PAIR_MAGENTA if opus > 0 else COLOR_PAIR_DEFAULT
    line2 = (
        f"  Bugs: "
    )
    stdscr.addstr(y, 1, line2, curses.color_pair(COLOR_PAIR_DEFAULT))
    stdscr.addstr(y, 1 + len(line2), f"{bugs} confirmados",
                 curses.color_pair(bug_color) | curses.A_BOLD)
    pos = 1 + len(line2) + len(f"{bugs} confirmados")
    stdscr.addstr(y, pos, "  │  Fixes: ", curses.color_pair(COLOR_PAIR_DEFAULT))
    pos += len("  │  Fixes: ")
    stdscr.addstr(y, pos, f"{fixes} OK / {s.get('fixes_attempted', 0)} att / "
                          f"{s.get('fixes_reverted', 0)} rev",
                 curses.color_pair(fix_color) | curses.A_BOLD)
    pos += len(f"{fixes} OK / {s.get('fixes_attempted', 0)} att / "
               f"{s.get('fixes_reverted', 0)} rev")
    stdscr.addstr(y, pos, "  │  Opus: ", curses.color_pair(COLOR_PAIR_DEFAULT))
    pos += len("  │  Opus: ")
    stdscr.addstr(y, pos, f"{opus} invocaciones", curses.color_pair(opus_color) | curses.A_BOLD)
    y += 1
    stdscr.addstr(y, 1, '─' * (w - 2), gray_attr())
    return y + 1


def draw_cadence_panel(stdscr, counts: Counter, y: int, height: int) -> int:
    """Panel de cadencia por modelo (calls en últimos 30s)."""
    h, w = stdscr.getmaxyx()
    if y + height >= h:
        return y
    stdscr.addstr(y, 1, "─── Cadencia por modelo (últimos 30s) ───",
                 curses.color_pair(COLOR_PAIR_HEADER) | curses.A_BOLD)
    y += 1
    if not counts:
        stdscr.addstr(y, 3, "(sin actividad en los últimos 30s)",
                     gray_attr())
        return y + 1
    max_count = max(counts.values()) or 1
    bar_width = max(8, w - 50)
    # Ordenar: locales primero, luego cloud.
    sorted_counts = sorted(
        counts.items(),
        key=lambda kv: (not kv[0].startswith('ollama:'), -kv[1], kv[0]),
    )
    for model_id, count in sorted_counts[:height - 1]:
        label = model_family_short(model_id)
        bar_len = int((count / max_count) * bar_width)
        bar = '█' * bar_len + '·' * (bar_width - bar_len)
        color = model_color(model_id)
        line = f"  {label:14s} {count:>4}  "
        stdscr.addstr(y, 1, line, color)
        try:
            stdscr.addstr(y, 1 + len(line), bar, color | curses.A_BOLD)
        except curses.error:
            pass
        y += 1
        if y >= h - 2:
            break
    return y + 1


def draw_system_panel(stdscr, cpu: dict, gpus: list, ollama: list, y: int) -> int:
    """Panel de CPU/GPU/Ollama."""
    h, w = stdscr.getmaxyx()
    if y + 8 >= h:
        return y
    stdscr.addstr(y, 1, "─── Sistema ───", curses.color_pair(COLOR_PAIR_HEADER) | curses.A_BOLD)
    y += 1
    # CPU.
    stdscr.addstr(y, 1, "  CPU: ", curses.color_pair(COLOR_PAIR_DEFAULT))
    idle = cpu.get('idle_pct', '?')
    try:
        idle_f = float(idle)
        idle_color = COLOR_PAIR_GREEN if idle_f > 20 else (
            COLOR_PAIR_YELLOW if idle_f > 5 else COLOR_PAIR_RED)
    except (ValueError, TypeError):
        idle_color = COLOR_PAIR_DEFAULT
    stdscr.addstr(y, 7, f"idle {idle}%  ", curses.color_pair(idle_color) | curses.A_BOLD)
    stdscr.addstr(y, 19, f"load {cpu.get('load', '?')}",
                 gray_attr())
    y += 1
    # GPUs.
    for gpu in gpus:
        mem_pct = (gpu['mem_used_mib'] / gpu['mem_total_mib'] * 100) if gpu['mem_total_mib'] else 0
        util_color = COLOR_PAIR_GREEN if gpu['util_pct'] < 50 else (
            COLOR_PAIR_YELLOW if gpu['util_pct'] < 80 else COLOR_PAIR_RED)
        mem_color = COLOR_PAIR_GREEN if mem_pct < 60 else (
            COLOR_PAIR_YELLOW if mem_pct < 85 else COLOR_PAIR_RED)
        stdscr.addstr(y, 1, f"  GPU {gpu['index']} {gpu['name'][:18]:18s} ", curses.color_pair(COLOR_PAIR_DEFAULT))
        stdscr.addstr(y, 32, f"util={gpu['util_pct']:>3d}%  ", curses.color_pair(util_color) | curses.A_BOLD)
        stdscr.addstr(y, 46, f"vram {gpu['mem_used_mib']:>5d}MiB / {gpu['mem_total_mib']:>5d}MiB  ",
                     curses.color_pair(mem_color))
        bar_len = int(mem_pct / 100 * 15)
        bar = '█' * bar_len + '·' * (15 - bar_len)
        stdscr.addstr(y, 78, bar, mem_color)
        y += 1
        if y >= h - 2:
            break
    # Ollama.
    if ollama:
        stdscr.addstr(y, 1, "  Ollama: ", curses.color_pair(COLOR_PAIR_LOCAL) | curses.A_BOLD)
        y += 1
        for m in ollama[:3]:
            stdscr.addstr(y, 3, f"● {m['name']:30s}  vram={m['vram_gb']:.1f}GB",
                         curses.color_pair(COLOR_PAIR_LOCAL))
            y += 1
            if y >= h - 2:
                break
    return y + 1


def draw_log_panel(stdscr, log_buffer: LogBuffer, y: int, height: int) -> int:
    """Panel de log con scroll."""
    h, w = stdscr.getmaxyx()
    if y + 2 >= h:
        return y
    stdscr.addstr(y, 1, "─── Log (↑/↓ para scroll) ───",
                 curses.color_pair(COLOR_PAIR_HEADER) | curses.A_BOLD)
    y += 1
    if not log_buffer.lines:
        stdscr.addstr(y, 3, "(log vacío)", gray_attr())
        return y + 1
    # Mostrar las últimas N líneas que entren, con scroll.
    visible = height - 1
    lines_to_show = list(log_buffer.lines)[-visible:]
    for ts, line in lines_to_show:
        if y >= h - 1:
            break
        # Color por severidad.
        if 'error' in line.lower() or '⨯' in line:
            color = curses.color_pair(COLOR_PAIR_RED)
        elif 'warn' in line.lower() or 'stall' in line.lower():
            color = curses.color_pair(COLOR_PAIR_YELLOW)
        elif 'CONFIRMED' in line or 'OK' in line or 'done' in line:
            color = curses.color_pair(COLOR_PAIR_GREEN)
        elif '[call]' in line:
            color = curses.color_pair(COLOR_PAIR_CYAN)
        else:
            color = curses.color_pair(COLOR_PAIR_DEFAULT)
        try:
            # Calcular segundos desde ahora.
            ago = int(time.time() - ts)
            prefix = f"-{ago:>3d}s  " if ago < 60 else f"-{ago//60}m{ago%60:02d}s  "
            stdscr.addstr(y, 1, prefix, gray_attr())
            stdscr.addstr(y, 1 + len(prefix), line[:w - 2 - len(prefix)], color)
        except curses.error:
            pass
        y += 1
    return y


def draw_footer(stdscr, paused: bool, layout: str):
    h, w = stdscr.getmaxyx()
    stdscr.addstr(h - 1, 1, '═' * (w - 2), curses.color_pair(COLOR_PAIR_HEADER))
    keys = "  q:salir  r:refresh  p:pausar({})  l:layout({})  ?:ayuda  ".format(
        "ON" if paused else "off", layout,
    )
    stdscr.addstr(h - 1, max(1, (w - len(keys)) // 2), keys,
                 gray_attr())


def main(stdscr):
    # Validar tamaño de terminal.
    h, w = stdscr.getmaxyx()
    if h < 20 or w < 80:
        # Terminal muy pequeña — avisar y salir.
        stdscr.addstr(0, 0, f"Terminal muy pequeña ({h}x{w}). Mínimo 20x80.")
        stdscr.addstr(1, 0, "Resize la terminal y vuelve a lanzar la TUI.")
        stdscr.refresh()
        stdscr.getch()
        return

    init_colors()
    stdscr.nodelay(True)  # getch() no bloquea
    try:
        curses.curs_set(0)  # ocultar cursor (puede fallar en algunas terminales)
    except curses.error:
        pass  # Algunas terminales no soportan ocultar el cursor.

    log = LogBuffer()
    last_refresh = 0.0
    last_status = {}
    last_cpu = {}
    last_gpus = []
    last_ollama = []
    last_counts: Counter = Counter()
    harness_pid = int(open(HARNESS_PID_FILE).read().strip()) if os.path.exists(HARNESS_PID_FILE) else 0

    paused = False
    layout = 'full'  # 'full' | 'compact' | 'metrics'
    cycle_duration = int(os.environ.get('LOOP_HOURS', '8'))  # horas del ciclo
    cycle_launched = False  # true cuando lanzamos un ciclo desde la TUI

    # --- helpers TUI ---
    def _stop_harness():
        """Mata el harness si está corriendo."""
        if os.path.exists(HARNESS_PID_FILE):
            try:
                pid = int(open(HARNESS_PID_FILE).read().strip())
                os.kill(pid, 15)  # SIGTERM
                time.sleep(0.5)
                if check_alive(pid):
                    os.kill(pid, 9)  # SIGKILL
                os.remove(HARNESS_PID_FILE)
            except (OSError, ValueError):
                pass

    def _launch_harness(wt_dir: str = '/tmp/wt-night', subdir: str = 'apps/dashboard',
                        hours: int = 8, dev_url: str = ''):
        """Lanza el harness en background con nohup."""
        env = os.environ.copy()
        env['LOOP_WT'] = wt_dir
        env['LOOP_SUBDIR'] = subdir
        env['LOOP_HOURS'] = str(hours)
        if dev_url:
            env['LOOP_DEV_URL'] = dev_url
        harness_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'harness.py')
        cmd = ['nohup', 'python3', '-u', harness_py,
               '--no-e2e',  # por ahora sin E2E (se puede cambiar)
               ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=open(LOG_FILE, 'a'),  # append al log
                stderr=subprocess.STDOUT,
                env=env,
                cwd=os.path.dirname(os.path.abspath(__file__)),
                preexec_fn=os.setpgrp,  # grupo propio para no heredar CTRL+C
            )
            with open(HARNESS_PID_FILE, 'w') as f:
                f.write(str(proc.pid))
            return proc.pid
        except Exception as e:
            return 0

    def _show_duration_menu(stdscr, current: int) -> int:
        """Muestra un menú de selección de duración."""
        options = [(1, '1 hora'), (2, '2 horas'), (4, '4 horas'),
                   (8, '8 horas'), (12, '12 horas'), (24, '24 horas'),
                   (0, 'Indefinido (hasta CTRL+C)')]
        sel = 0
        for i, (hours, _) in enumerate(options):
            if hours == current:
                sel = i
                break
        while True:
            h, w = stdscr.getmaxyx()
            menu_h = len(options) + 3
            menu_y = max(2, (h - menu_h) // 2)
            menu_x = max(2, (w - 30) // 2)
            stdscr.clear()
            for i, (_, label) in enumerate(options):
                prefix = '▸ ' if i == sel else '  '
                color = (curses.color_pair(COLOR_PAIR_GREEN) | curses.A_BOLD
                         if i == sel else curses.color_pair(COLOR_PAIR_DEFAULT))
                stdscr.addstr(menu_y + i, menu_x, f'{prefix}{label}', color)
            stdscr.addstr(menu_y + len(options), menu_x, 'ENTER: confirmar  ESC: cancelar',
                          curses.color_pair(COLOR_PAIR_DEFAULT) | curses.A_DIM)
            k = stdscr.getch()
            if k == curses.KEY_UP:
                sel = (sel - 1) % len(options)
            elif k == curses.KEY_DOWN:
                sel = (sel + 1) % len(options)
            elif k in (ord('\n'), ord('\r'), curses.KEY_ENTER):
                return options[sel][0]
            elif k == 27:  # ESC
                return current
            time.sleep(0.01)

    while True:
        key = stdscr.getch()
        if key in (ord('q'), ord('Q')):
            return
        elif key in (ord('r'), ord('R')):
            last_refresh = 0  # forzar refresh
        elif key in (ord('p'), ord('P')):
            paused = not paused
        elif key in (ord('c'), ord('C')):
            log.lines.clear()
        elif key in (ord('l'), ord('L')):
            layout = {'full': 'compact', 'compact': 'metrics', 'metrics': 'full'}[layout]
        elif key == curses.KEY_UP:
            log.scroll_up(3)
        elif key == curses.KEY_DOWN:
            log.scroll_down(3)
        elif key in (ord('?'),):
            stdscr.clear()
            stdscr.addstr(0, 1, "TUI v6 — Teclas", curses.color_pair(COLOR_PAIR_HEADER) | curses.A_BOLD)
            help_lines = [
                "  q / Q     salir",
                "  r / R     forzar refresh inmediato",
                "  p / P     pausar / reanudar auto-refresh",
                "  c / C     limpiar log mostrado",
                "  l / L     cambiar layout (full / compact / metrics)",
                "  s / S     lanzar / detener ciclo del harness",
                "  d / D     cambiar duración del ciclo",
                f"            → actual: {cycle_duration}h {'(indefinido)' if cycle_duration == 0 else ''}",
                "  ↑ / ↓     scroll en el log",
                "  ?         esta ayuda (cualquier tecla para volver)",
            ]
            for i, hl in enumerate(help_lines, 1):
                stdscr.addstr(i, 1, hl, curses.color_pair(COLOR_PAIR_DEFAULT))
            stdscr.getch()
            stdscr.clear()
        elif key in (ord('s'), ord('S')):
            # Toggle: si está corriendo → parar; si no → lanzar.
            if harness_pid and check_alive(harness_pid):
                log.lines.append(f'[TUI] deteniendo harness (PID {harness_pid})...')
                _stop_harness()
                harness_pid = 0
                cycle_launched = False
                log.lines.append('[TUI] harness detenido.')
            else:
                log.lines.append(f'[TUI] lanzando ciclo del harness ({cycle_duration}h '
                                 f'en /tmp/wt-night/apps/dashboard)...')
                pid = _launch_harness(hours=cycle_duration)
                if pid:
                    harness_pid = pid
                    cycle_launched = True
                    log.lines.append(f'[TUI] harness lanzado con PID {pid}, duración {cycle_duration}h')
                else:
                    log.lines.append('[TUI] ERROR: no se pudo lanzar el harness.')
            last_refresh = 0
        elif key in (ord('d'), ord('D')):
            # Menú de selección de duración.
            curses.curs_set(1)
            cycle_duration = _show_duration_menu(stdscr, cycle_duration)
            curses.curs_set(0)
            log.lines.append(f'[TUI] duración del ciclo: {cycle_duration}h '
                             f'{"(indefinido)" if cycle_duration == 0 else ""}')
            stdscr.clear()
            last_refresh = 0

        # Refresh automático.
        if not paused and (time.time() - last_refresh) >= REFRESH_SEC:
            last_refresh = time.time()
            log.update(LOG_FILE)
            last_status = read_status(STATUS_FILE)
            last_status['pid'] = harness_pid
            last_counts = read_log_calls(LOG_FILE, window_sec=30)
            last_cpu = get_cpu_stats()
            last_gpus = get_gpu_stats()
            last_ollama = get_ollama_models()
        # Render.
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        # Header.
        harness_alive = harness_pid and check_alive(harness_pid)
        y = draw_header(stdscr, last_status, harness_alive, y=0)
        # Layouts.
        if layout == 'full':
            # Cadencia (40% de la pantalla disponible).
            cad_h = max(4, (h - y) // 3)
            y = draw_cadence_panel(stdscr, last_counts, y, cad_h)
            # Sistema.
            sys_h = 8
            y = draw_system_panel(stdscr, last_cpu, last_gpus, last_ollama, y)
            # Log.
            y = draw_log_panel(stdscr, log, y, h - y - 2)
        elif layout == 'compact':
            # Una sola línea de cadencia + sistema + log completo.
            cad_h = max(3, len(last_counts) + 2)
            y = draw_cadence_panel(stdscr, last_counts, y, cad_h)
            y = draw_system_panel(stdscr, last_cpu, last_gpus, last_ollama, y)
            y = draw_log_panel(stdscr, log, y, h - y - 2)
        else:  # metrics
            # Solo cadencia + sistema.
            cad_h = max(4, len(last_counts) + 2)
            y = draw_cadence_panel(stdscr, last_counts, y, cad_h)
            y = draw_system_panel(stdscr, last_cpu, last_gpus, last_ollama, y)
            y += 1
            stdscr.addstr(y, 1, "  (layout 'metrics': log oculto — pulsa 'l' para cambiar)",
                         gray_attr())
        # Footer.
        draw_footer(stdscr, paused, layout)
        stdscr.refresh()
        time.sleep(0.1)


if __name__ == '__main__':
    # Sanity check: curses solo funciona con un TTY real.
    if not sys.stdout.isatty():
        print("Error: la TUI requiere un TTY real (no funciona redirigido).",
              file=sys.stderr)
        print("Lánzala desde una terminal interactiva.", file=sys.stderr)
        sys.exit(1)
    # Verificar TERM.
    if not os.environ.get('TERM'):
        os.environ['TERM'] = 'xterm-256color'
    try:
        # curses.wrapper se encarga de initscr/endwin/cbreak/nocbreak automáticamente.
        # Si algo falla dentro de main, el cleanup es robusto.
        curses.wrapper(main)
    except KeyboardInterrupt:
        # Restaurar terminal explícitamente (a veces curses.wrapper no lo hace
        # del todo bien cuando hay un KeyboardInterrupt dentro de nodelay).
        try:
            curses.nocbreak()
            curses.endwin()
        except curses.error:
            pass
    except curses.error as e:
        print(f"\n[Error de curses: {e}]", file=sys.stderr)
        print("La TUI requiere ncurses. En Linux: sudo apt install libncurses-dev",
              file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\n[Error inesperado: {e}]", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
