#!/usr/bin/env python3
"""
tui.py — TUI interactiva del harness v7 (curses).

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
  TAB      ciclar entre tabs (Dashboard / Models / Logs / Stats)
  1..4     ir directo a una tab
  l / L    cambiar layout (legacy: compact/full/metrics)
  m / M    configurar modelos por tier
  s / S    lanzar / detener ciclo del harness
  d / D    cambiar duración del ciclo (1h/2h/4h/8h/indefinido)
  ?        ayuda
  ↑/↓      scroll en el panel de log

Tabs:
  Dashboard   Cuadrícula de contenedores por thread (hunters, filters, judges, fixers)
              con modelo, archivo actual, status, calls.
  Models      Cadencia por modelo + sistema (CPU/GPU/Ollama).
  Logs        Log completo a pantalla completa.
  Stats       Contadores y memoria del harness.

USO:
  ./tui.py [STATUS_FILE] [LOG_FILE]
    defaults: /tmp/loop-v6-status.json, /tmp/loop-v6.log
"""
from __future__ import annotations

import curses
import os
import subprocess
import sys
import time
from collections import Counter

from loop_harness import tui_core

STATUS_FILE = sys.argv[1] if len(sys.argv) > 1 else '/tmp/loop-v6-status.json'
LOG_FILE = sys.argv[2] if len(sys.argv) > 2 else '/tmp/loop-v6.log'
HARNESS_PID_FILE = '/tmp/loop-v6.pid'
MODELS_CONFIG_FILE = '/tmp/harness-models.json'
OLLAMA_URL = 'http://host.docker.internal:11434'

REFRESH_SEC = 2.0
LOG_LINES = 200  # cuántas líneas del log mantener en memoria

# Schema version esperado del status JSON (producido por harness.write_status).
SCHEMA_VERSION = 2

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


LogBuffer = tui_core.LogBuffer


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
    return tui_core.model_family_short(model_id)


def read_status(path: str) -> dict:
    return tui_core.read_status(path, schema_version=SCHEMA_VERSION)


def _safe_int(val, default: int = 0) -> int:
    return tui_core.safe_int(val, default)


def _read_harness_pid() -> int:
    return tui_core.read_harness_pid(HARNESS_PID_FILE)


def _safe_float(val, default: float = 0.0) -> float:
    return tui_core.safe_float(val, default)


def _safe_get_funnel(status: dict) -> dict:
    return tui_core.safe_get_funnel(status)


def _safe_get_roles(status: dict) -> list:
    return tui_core.safe_get_roles(status)


def _safe_get_nodes(status: dict) -> list:
    return tui_core.safe_get_nodes(status)


def _safe_get_threads(status: dict) -> list:
    return tui_core.safe_get_threads(status)


def read_log_calls(path: str, window_sec: int = 30) -> Counter:
    return tui_core.read_log_calls(path, window_sec)


def get_cpu_stats() -> dict:
    return tui_core.get_cpu_stats()


def get_gpu_stats() -> list[dict]:
    return tui_core.get_gpu_stats()


def get_ollama_models() -> list[dict]:
    return tui_core.get_ollama_models(OLLAMA_URL)


def check_alive(pid: int) -> bool:
    return tui_core.check_alive(pid)


# ---------------------------------------------------------------------------
# Configuración de modelos por tier (tecla M en la TUI)
# ---------------------------------------------------------------------------

def _load_model_config(path: str) -> dict:
    return tui_core.load_model_config(path)


def _save_model_config(path: str, config: dict) -> None:
    tui_core.save_model_config(path, config)


def _default_model_config() -> dict:
    return tui_core.default_model_config()


def _show_model_config(stdscr, config: dict) -> dict:
    """Pantalla de selección de modelos por tier. Devuelve la nueva config."""
    h, w = stdscr.getmaxyx()
    tiers_list = [
        ('Tier 0 · Hunters local', 'tier0_local', 'Cazan bugs (locales)'),
        ('Tier 0 · Hunters cloud', 'tier0_cloud', 'Cazan bugs (chiquitos)'),
        ('Tier 1 · Filtros', 'tier1_filters', 'Refutan ruido (baratos)'),
        ('Tier 2 · Jueces', 'tier2_judges', 'Confirman bugs (medios)'),
        ('Tier 3 · Fixers', 'tier3_fixers', 'Generan fixes (fuertes)'),
        ('Tier 4 · Árbitros', 'tier4_arbiters', 'Veredicto final (riesgosos)'),
    ]
    # Construir lista plana de (tier_label, tier_key, model_id, enabled)
    items: list[tuple[str, str, str, bool]] = []
    for tlabel, tkey, tdesc in tiers_list:
        all_models = _default_model_config()[tkey]  # todos los disponibles
        selected = set(config.get(tkey, []))
        items.append(('HEADER', tkey, f'{tlabel} — {tdesc} ({len(selected)}/{len(all_models)})', False))
        for mid in all_models:
            items.append(('MODEL', tkey, mid, mid in selected))

    sel = 0
    scroll = 0
    visible = h - 4  # espacio para header + footer
    while True:
        stdscr.erase()
        # Header.
        header = 'CONFIGURACIÓN DE MODELOS POR TIER'
        stdscr.addstr(0, max(1, (w - len(header)) // 2), header[:w - 2],
                      curses.color_pair(COLOR_PAIR_HEADER) | curses.A_BOLD)
        # Items.
        for i in range(scroll, min(len(items), scroll + visible)):
            vi = i - scroll + 2  # dejar 2 líneas para header
            ttype, _, label, enabled = items[i]
            checkbox = ' [✓]' if enabled else ' [ ]'
            prefix = '▸' if i == sel else ' '
            attr = curses.color_pair(COLOR_PAIR_DEFAULT)
            if i == sel:
                attr |= curses.A_REVERSE
            if ttype == 'HEADER':
                attr |= curses.A_BOLD
                prefix = '▾' if i == sel else ' '
                checkbox = ''
            line = f' {prefix}{checkbox} {label}'[:w - 2]
            stdscr.addstr(vi, 1, line, attr)
        # Footer.
        footer = ('ESPACIO: toggle  ENTER: guardar  ESC: cancelar  '
                  '↑/↓: mover  A: todos  N: ninguno')
        stdscr.addstr(h - 1, 1, footer[:w - 2],
                      curses.color_pair(COLOR_PAIR_DEFAULT) | curses.A_DIM)
        stdscr.refresh()

        key = stdscr.getch()
        if key == 27:  # ESC
            return config
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER):
            # Guardar: reconstruir config desde items.
            new_config = {}
            for tlabel, tkey, _ in tiers_list:
                new_config[tkey] = [mid for ttype, mk, mid, en in items
                                     if ttype == 'MODEL' and mk == tkey and en]
            _save_model_config(MODELS_CONFIG_FILE, new_config)
            return new_config
        elif key == curses.KEY_UP:
            sel = (sel - 1) % len(items)
            if sel < scroll:
                scroll = sel
        elif key == curses.KEY_DOWN:
            sel = (sel + 1) % len(items)
            if sel >= scroll + visible:
                scroll = sel - visible + 1
        elif key == ord(' '):
            ttype, tkey, mid, enabled = items[sel]
            if ttype == 'MODEL':
                items[sel] = ('MODEL', tkey, mid, not enabled)
            elif ttype == 'HEADER':
                # Toggle todos los modelos del tier.
                new_en = not any(en for tt, tk, _, en in items
                                 if tt == 'MODEL' and tk == tkey)
                for j, (tt, tk, m, _) in enumerate(items):
                    if tt == 'MODEL' and tk == tkey:
                        items[j] = ('MODEL', tkey, m, new_en)
        elif key in (ord('a'), ord('A')):
            # Enable all.
            for j in range(len(items)):
                if items[j][0] == 'MODEL':
                    items[j] = ('MODEL', items[j][1], items[j][2], True)
        elif key in (ord('n'), ord('N')):
            # Disable all.
            for j in range(len(items)):
                if items[j][0] == 'MODEL':
                    items[j] = ('MODEL', items[j][1], items[j][2], False)


def draw_header(stdscr, status: dict, harness_alive: bool, y: int = 0) -> int:
    """Header con el estado del harness. Retorna la siguiente línea libre.
    Soporta schema v1 (stats) y v2 (funnel). Badge schema vN en header."""
    h, w = stdscr.getmaxyx()
    sv = _safe_int(status.get('schema_version', 1), 1)
    if harness_alive:
        title = f"HARNESS v7 — ACTIVO (PID {status.get('pid', '?')}) [schema v{sv}]"
        title_color = curses.color_pair(COLOR_PAIR_GREEN) | curses.A_BOLD
    else:
        title = f"HARNESS v7 — MUERTO (PID {status.get('pid', '?')}) [schema v{sv}]"
        title_color = curses.color_pair(COLOR_PAIR_RED) | curses.A_BOLD

    stdscr.addstr(y, 1, '═' * (w - 2), curses.color_pair(COLOR_PAIR_HEADER))
    stdscr.addstr(y, max(1, (w - len(title)) // 2), title, title_color)
    y += 1
    # Leer de funnel (v2) con fallback a stats (v1)
    f = _safe_get_funnel(status)
    up = _safe_int(status.get('uptime_sec', 0))
    h_, m_, s_ = up // 3600, (up % 3600) // 60, up % 60
    uptime_str = f"{h_:02d}:{m_:02d}:{s_:02d}"
    line1 = (
        f"  Uptime: {uptime_str}  |  Round: {f.get('rounds', 0)}  "
        f"|  Clean: {f.get('clean_rounds_in_a_row', 0)}  "
        f"|  Suspects: {f.get('suspects_produced', 0)}  "
        f"|  Refutados: {f.get('suspects_refuted', f.get('suspects_refuted_by_filter', 0))}"
    )
    try:
        stdscr.addstr(y, 1, line1[:w - 2], curses.color_pair(COLOR_PAIR_DEFAULT))
    except curses.error:
        pass
    y += 1
    bugs = _safe_int(f.get('bugs_confirmed', 0))
    bug_color = COLOR_PAIR_RED if bugs > 0 else COLOR_PAIR_DEFAULT
    fixes = _safe_int(f.get('fixes_succeeded', 0))
    fix_color = COLOR_PAIR_GREEN if fixes > 0 else COLOR_PAIR_DEFAULT
    opus = _safe_int(f.get('opus_invocations', 0))
    opus_color = COLOR_PAIR_MAGENTA if opus > 0 else COLOR_PAIR_DEFAULT
    try:
        fixes_str = f"{fixes}/{f.get('fixes_attempted', 0)} att"
        line2 = f"  Bugs: {bugs} confirm  |  FixOK: {fixes_str}  |  Opus: {opus}"
        stdscr.addstr(y, 1, line2[:w - 2], curses.color_pair(COLOR_PAIR_DEFAULT))
    except curses.error:
        pass
    y += 1
    try:
        stdscr.addstr(y, 1, '─' * (w - 2), gray_attr())
    except curses.error:
        pass
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
        mem_color = COLOR_PAIR_GREEN if mem_pct < 60 else (
            COLOR_PAIR_YELLOW if mem_pct < 85 else COLOR_PAIR_RED)

        bar_len = int(mem_pct / 100 * 15)
        bar = '█' * bar_len + '·' * (15 - bar_len)

        # Construir la línea completa sin depender de columnas fijas
        line = f"  GPU {gpu['index']} {gpu['name'][:18]:18s} util={gpu['util_pct']:>3d}%  vram {gpu['mem_used_mib']:>5d}MiB / {gpu['mem_total_mib']:>5d}MiB  {bar}"

        # Truncar a w-2 y realizar un único addstr
        try:
            stdscr.addstr(y, 1, line[:w-2], curses.color_pair(mem_color))
        except curses.error:
            pass

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


def draw_footer(stdscr, paused: bool, current_tab: str):
    h, w = stdscr.getmaxyx()
    stdscr.addstr(h - 1, 1, '═' * (w - 2), curses.color_pair(COLOR_PAIR_HEADER))
    keys = "  q:salir  r:refresh  p:pausar({})  TAB:tab({})  m:modelos  s:start/stop  d:duración  ?:ayuda  ".format(
        "ON" if paused else "off", current_tab,
    )
    stdscr.addstr(h - 1, max(1, (w - len(keys)) // 2), keys,
                 gray_attr())


# ---------------------------------------------------------------------------
# Tabs y vistas nuevas (jul-2026: dashboard con contenedores por proceso).
# ---------------------------------------------------------------------------

TABS = [
    ('dashboard', '⟦Dashboard⟧'),
    ('models',    'Modelos'),
    ('logs',      'Logs'),
    ('stats',     'Stats'),
]


def draw_tabs_header(stdscr, current_tab: str, y: int = 0) -> int:
    """Dibuja la barra de tabs en la parte superior. Devuelve la siguiente Y libre."""
    h, w = stdscr.getmaxyx()
    line = '  '
    for tab_id, label in TABS:
        if tab_id == current_tab:
            line += f'⟦{label}⟧  '
        else:
            line += f' {label}  '
    line += '   (TAB: cambiar)'
    stdscr.addstr(y, 1, line[:w - 2], curses.color_pair(COLOR_PAIR_HEADER) | curses.A_BOLD)
    stdscr.addstr(y + 1, 1, '─' * (w - 2), gray_attr())
    return y + 2


def _short_model(mid: str) -> str:
    return tui_core.short_model(mid)


def draw_process_containers(stdscr, status: dict, y: int) -> int:
    """Dashboard ultra-compacto: 1 fila por tier + orquestador. Iconos de estado.

    Diseño:
      🔍 Locales    🟢🟢🟢⚪⚪  30B×2, CPU×2                        12 calls
      ☁️ Cloud      🟢🟢🟢🟢⚪⚪  flash-lite, dsv4-free, ...          8 calls
      🔬 Filtros    🟢🟢🟢🟢🟢🟢  m2.7, flash-lite, n-mini, ...     45 calls
      ⚖️ Jueces     🟢🟢⚪  m3, kimi-k2.7, dsv4-pro                    3 calls
      🧠 Orquest.   ✓ 30 prompts, 2 journeys                          (cache)
    """
    h, w = stdscr.getmaxyx()
    threads = status.get('threads', [])

    # Iconos de estado.
    BUSY_ICON = '●'   # 🟢 activo
    IDLE_ICON = '○'   # ⚪ inactivo

    ORDER = [
        ('hunter', '🔍 Locales', '🟤'),
        ('cloud',  '☁️ Cloud',   '🔵'),
        ('filter', '🔬 Filtros', '🟣'),
        ('judge',  '⚖️ Jueces',  '🟡'),
        ('fixer',  '🔧 Fixers',  '🔴'),
    ]

    grouped: dict[str, list[dict]] = {}
    for t in threads:
        grouped.setdefault(t.get('role') or t.get('kind', '?'), []).append(t)

    for cat_key, label, _ in ORDER:
        agents = grouped.get(cat_key, [])
        if not agents:
            continue
        busy_n = sum(1 for a in agents if a.get('status') == 'busy')
        total = len(agents)
        calls = sum(a.get('calls', 0) for a in agents)

        # Fila de iconos de status.
        icons = ''
        for a in agents[:10]:
            icons += BUSY_ICON if a.get('status') == 'busy' else IDLE_ICON
        if total > 10:
            icons += f' +{total - 10}'

        # Modelos únicos abreviados.
        models = sorted(set(_short_model(a.get('model', '?')) for a in agents))
        model_str = ', '.join(models[:5])
        if len(models) > 5:
            model_str += f' +{len(models) - 5}'

        line = f' {label:14s} {icons:14s} {model_str[:45]:45s} {calls}c'
        try:
            attr = curses.color_pair(COLOR_PAIR_DEFAULT)
            if busy_n > 0:
                attr |= curses.A_BOLD
            stdscr.addstr(y, 1, line[:w - 2], attr)
        except curses.error:
            pass
        y += 1

        # Archivos actuales (solo si hay busy).
        if busy_n > 0:
            busy_list = [a for a in agents if a.get('status') == 'busy']
            files_line = '   └─ '
            for a in busy_list[:3]:
                fn = (a.get('current_file', '') or '?').split('/')[-1][:22]
                files_line += f'{a["name"]}:{fn}  '
            try:
                stdscr.addstr(y, 1, files_line[:w - 2], gray_attr())
            except curses.error:
                pass
            y += 1

    # Orquestador.
    orch = status.get('orchestrator', {}) or {}
    # FIX crash: schema v2 usa hunting_prompts_n (int directo). Fallback a
    # hunting_prompts si el campo es int (schema v1 en transicion).
    # NUNCA hacemos len() sobre un campo que puede ser int.
    orch_prompts_n = orch.get('hunting_prompts_n')
    if orch_prompts_n is None:
        # Compatibilidad v1: el campo puede ser int o lista
        raw = orch.get('hunting_prompts', 0)
        orch_prompts_n = len(raw) if isinstance(raw, (list, tuple)) else _safe_int(raw, 0)
    else:
        orch_prompts_n = _safe_int(orch_prompts_n, 0)
    orch_journeys_n = orch.get('e2e_journeys_n')
    if orch_journeys_n is None:
        raw = orch.get('e2e_journeys', 0)
        orch_journeys_n = len(raw) if isinstance(raw, (list, tuple)) else _safe_int(raw, 0)
    else:
        orch_journeys_n = _safe_int(orch_journeys_n, 0)
    if orch_prompts_n or orch_journeys_n:
        orch_line = f' Orquest.  {orch_prompts_n} prompts, {orch_journeys_n} journeys'
        try:
            stdscr.addstr(y, 1, orch_line[:w - 2], curses.color_pair(COLOR_PAIR_DEFAULT))
        except curses.error:
            pass
        y += 1
    return y


def draw_nodes_panel(stdscr, status: dict, y: int) -> int:
    """Panel NODOS: por nodo muestra GPUs con util%/VRAM/modelo cargado.
    Schema v2: lee status['nodes'] (lista). Si el harness no ha escrito nodes aun,
    muestra placeholder. Seguro: nunca llama len() sobre int."""
    h, w = stdscr.getmaxyx()
    nodes = _safe_get_nodes(status)
    if not nodes:
        try:
            stdscr.addstr(y, 1, '[ NODOS: sin datos (harness no activo o schema v1) ]',
                          gray_attr())
        except curses.error:
            pass
        return y + 1
    try:
        stdscr.addstr(y, 1, '─── NODOS ─────────────────────────────────────────', gray_attr())
    except curses.error:
        pass
    y += 1
    for node in nodes:
        if y >= h - 2:
            break
        alive = node.get('alive', False)
        bullet = '*' if alive else 'o'
        bullet_color = curses.color_pair(COLOR_PAIR_GREEN) if alive else curses.color_pair(COLOR_PAIR_RED)
        host = tui_core.node_host_label(node)[:30]
        latency = node.get('latency_ms', 0)
        node_line = f' {host}  {latency}ms'
        try:
            stdscr.addstr(y, 1, bullet, bullet_color | curses.A_BOLD)
            stdscr.addstr(y, 3, node_line[:w - 5], curses.color_pair(COLOR_PAIR_DEFAULT))
        except curses.error:
            pass
        y += 1
        # GPUs del nodo
        gpus = node.get('gpus', []) or []
        for g in gpus:
            if y >= h - 2:
                break
            gi = _safe_int(g.get('index', 0))
            gname = str(g.get('name', '?'))[:18]
            util = _safe_int(g.get('util_pct', 0))
            used_mib = _safe_int(g.get('vram_used_mib', 0))
            total_mib = _safe_int(g.get('vram_total_mib', 1)) or 1
            used_gb = used_mib / 1024
            total_gb = total_mib / 1024
            bar_width = 12
            filled = int(bar_width * used_mib / total_mib)
            bar = '#' * filled + '.' * (bar_width - filled)
            loaded = g.get('loaded_models', []) or []
            model_str = loaded[0][:16] if loaded else ('(libre)' if used_mib < 500 else '')
            gpu_line = (f'   GPU{gi} {gname:<18s} {util:>3}%  '
                        f'{used_gb:.1f}/{total_gb:.1f}GB [{bar}] {model_str}')
            try:
                stdscr.addstr(y, 1, gpu_line[:w - 2], curses.color_pair(COLOR_PAIR_DEFAULT))
            except curses.error:
                pass
            y += 1
        # Modelos Ollama cargados
        ollama_models = node.get('ollama_models', []) or []
        if ollama_models:
            om_str = '   Ollama: ' + ', '.join(
                f'{m["name"]}({m.get("vram_gb", 0):.1f}GB)' for m in ollama_models[:4]
            )
            try:
                stdscr.addstr(y, 1, om_str[:w - 2], gray_attr())
            except curses.error:
                pass
            y += 1
    return y


def draw_pipeline_panel(stdscr, status: dict, y: int) -> int:
    """Panel PIPELINE: itera status['roles'] (schema v2), una fila por rol.
    Contadores etiquetados (live N/M, busy B, calls C, /min M). Mata el '20/6'."""
    h, w = stdscr.getmaxyx()
    roles = _safe_get_roles(status)
    if not roles:
        # Fallback: delegate to legacy draw_process_containers logic omitted here
        # (the old function still exists for the dashboard tab).
        try:
            stdscr.addstr(y, 1, '[ PIPELINE: sin datos roles[] (schema v1?) ]', gray_attr())
        except curses.error:
            pass
        return y + 1
    try:
        stdscr.addstr(y, 1, '─── PIPELINE ──────────────────────────────────────', gray_attr())
    except curses.error:
        pass
    y += 1
    ROLE_ICON = {'hunter': 'H', 'filter': 'F', 'judge': 'J', 'fixer': 'X', 'cloud': 'C'}
    for role_entry in roles:
        if y >= h - 2:
            break
        role = role_entry.get('role', '?')
        label = role_entry.get('label', role)[:14]
        icon = ROLE_ICON.get(role, '?')
        live_n = _safe_int(role_entry.get('live_n', 0))
        conf_n = _safe_int(role_entry.get('configured_n', 0))
        busy_n = _safe_int(role_entry.get('busy_n', 0))
        quar_n = _safe_int(role_entry.get('quarantined_n', 0))
        calls = _safe_int(role_entry.get('calls_total', 0))
        cpm = _safe_float(role_entry.get('calls_per_min', 0.0), 0.0)
        to_n = _safe_int(role_entry.get('timeouts', 0))
        role_line = (f' [{icon}] {label:<14s}  live {live_n}/{conf_n}'
                     f'  busy:{busy_n}  q:{quar_n}  {calls}c  {cpm:.1f}/min  to:{to_n}')
        try:
            attr = curses.color_pair(COLOR_PAIR_DEFAULT)
            if busy_n > 0:
                attr |= curses.A_BOLD
            stdscr.addstr(y, 1, role_line[:w - 2], attr)
        except curses.error:
            pass
        y += 1
        # Sub-línea: modelos usados
        models = role_entry.get('models', []) or []
        if models:
            mods_str = '      ' + ', '.join(str(m).split(':')[-1][:14] for m in models[:5])
            if len(models) > 5:
                mods_str += f' +{len(models) - 5}'
            try:
                stdscr.addstr(y, 1, mods_str[:w - 2], gray_attr())
            except curses.error:
                pass
            y += 1
    return y


def draw_funnel_panel(stdscr, status: dict, y: int) -> int:
    """Panel EMBUDO: suspects→refutados→FP→confirmados→fixes con mini-barras.
    Lee status['funnel'] (schema v2). Seguro: todos los campos via _safe_int."""
    h, w = stdscr.getmaxyx()
    f = _safe_get_funnel(status)
    try:
        stdscr.addstr(y, 1, '─── EMBUDO ─────────────────────────────────────────', gray_attr())
    except curses.error:
        pass
    y += 1
    suspects = _safe_int(f.get('suspects_produced', 0))
    refuted = _safe_int(f.get('suspects_refuted', f.get('suspects_refuted_by_filter', 0)))
    fp = _safe_int(f.get('suspects_known_fp', f.get('suspects_filtered_as_known_fp', 0)))
    confirmed = _safe_int(f.get('bugs_confirmed', 0))
    fixes_att = _safe_int(f.get('fixes_attempted', 0))
    fixes_ok = _safe_int(f.get('fixes_succeeded', 0))
    fixes_rev = _safe_int(f.get('fixes_reverted', 0))
    to_review = _safe_int(f.get('fixes_to_review', 0))
    e2e = _safe_int(f.get('e2e_findings', 0))
    opus = _safe_int(f.get('opus_invocations', 0))

    bar_max = max(suspects, 1)
    BAR_W = 20

    def _bar(val: int) -> str:
        filled = int(BAR_W * val / bar_max)
        return '[' + '#' * filled + '.' * (BAR_W - filled) + ']'

    rows = [
        ('Suspects', suspects, COLOR_PAIR_DEFAULT),
        ('Refutados', refuted, COLOR_PAIR_YELLOW),
        ('FP conocidos', fp, COLOR_PAIR_GRAY),
        ('Confirmados', confirmed, COLOR_PAIR_RED),
        ('Fixes att/OK', f'{fixes_att}/{fixes_ok}', COLOR_PAIR_GREEN),
        ('Revertidos', fixes_rev, COLOR_PAIR_YELLOW),
        ('A revisar', to_review, COLOR_PAIR_MAGENTA),
        ('E2E findings', e2e, COLOR_PAIR_CYAN),
        ('Opus', opus, COLOR_PAIR_MAGENTA),
    ]
    for label, val, color in rows:
        if y >= h - 2:
            break
        if isinstance(val, int):
            bar = _bar(val)
            row_line = f'  {label:<14s} {val:>5}  {bar}'
        else:
            row_line = f'  {label:<14s} {str(val):>11}'
        try:
            stdscr.addstr(y, 1, row_line[:w - 2], curses.color_pair(color))
        except curses.error:
            pass
        y += 1
    return y


def draw_stats_panel(stdscr, status: dict, y: int) -> int:
    """Stats tab: muestra contadores, memoria, y el orcador."""
    h, w = stdscr.getmaxyx()
    stats = status.get('stats', {})
    memory = status.get('memory', {})
    uptime = status.get('uptime_sec', 0)
    lines = [
        f'  Uptime:  {uptime//3600:>3}h {(uptime%3600)//60:>2}m {uptime%60:>2}s',
        f'  Rounds:  {stats.get("rounds", 0)}  |  Clean in a row: {stats.get("clean_rounds_in_a_row", 0)}',
        f'  Suspects producidos:    {stats.get("suspects_produced", 0)}',
        f'  Refutados por filtro:   {stats.get("suspects_refuted_by_filter", 0)}',
        f'  FP conocidos:           {stats.get("suspects_filtered_as_known_fp", 0)}',
        f'  ─────────────────────────────────────────',
        f'  Bugs confirmados:       {stats.get("bugs_confirmed", 0)}',
        f'  Fixes intentados:       {stats.get("fixes_attempted", 0)}',
        f'  Fixes OK:               {stats.get("fixes_succeeded", 0)}',
        f'  Fixes revertidos:       {stats.get("fixes_reverted", 0)}',
        f'  Fixes a revisar:        {stats.get("fixes_to_review", 0)}',
        f'  E2E findings:           {stats.get("e2e_findings", 0)}',
        f'  Opus invocaciones:      {stats.get("opus_invocations", 0)}',
        f'  ─────────────────────────────────────────',
        f'  Memoria:',
        f'    Total rounds:    {memory.get("total_rounds", 0)}',
        f'    Bugs encontrados: {memory.get("total_bugs_found", 0)}',
        f'    Fixes aplicados: {memory.get("total_fixes_applied", 0)}',
        f'    Open bugs:       {memory.get("open_bugs", 0)}',
        f'    Zonas hardened:  {memory.get("hardened_zones", 0)}',
    ]
    for i, line in enumerate(lines):
        if y + i >= h - 1:
            break
        try:
            stdscr.addstr(y + i, 1, line[:w - 2], curses.color_pair(COLOR_PAIR_DEFAULT))
        except curses.error:
            pass
    return y + len(lines) + 1


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
    harness_pid = _read_harness_pid()

    paused = False
    current_tab = 'dashboard'  # 'dashboard' | 'models' | 'logs' | 'stats'
    layout = 'full'  # 'full' | 'compact' | 'metrics' (legacy, ahora se ignora)
    cycle_duration = int(os.environ.get('LOOP_HOURS', '8'))  # horas del ciclo
    cycle_launched = False  # true cuando lanzamos un ciclo desde la TUI
    model_config = _load_model_config(MODELS_CONFIG_FILE)

    # --- helpers TUI ---
    def _stop_harness():
        """Mata el harness si está corriendo."""
        pid = _read_harness_pid()
        if pid:
            try:
                os.kill(pid, 15)  # SIGTERM
                time.sleep(0.5)
                if check_alive(pid):
                    os.kill(pid, 9)  # SIGKILL
                if os.path.exists(HARNESS_PID_FILE):
                    os.remove(HARNESS_PID_FILE)
            except (OSError, ValueError):
                pass

    def _launch_harness(wt_dir: str = '/tmp/wt-night', subdir: str = 'apps/dashboard',
                        hours: int = 8, dev_url: str = ''):
        """Lanza el harness en background con nohup."""
        env = os.environ.copy()

        # Respetar variables de entorno como fallback si se pasan los valores por defecto
        if wt_dir == '/tmp/wt-night':
            wt_dir = env.get('LOOP_WT', wt_dir)
        if subdir == 'apps/dashboard':
            subdir = env.get('LOOP_SUBDIR', subdir)

        env['LOOP_WT'] = wt_dir
        env['LOOP_SUBDIR'] = subdir
        env['LOOP_HOURS'] = str(hours)
        if dev_url:
            env['LOOP_DEV_URL'] = dev_url

        harness_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'harness.py')
        cmd = ['nohup', 'python3', '-u', harness_py]
        if env.get('LOOP_E2E', '').lower() == 'true':
            cmd.append('--e2e')
        else:
            cmd.append('--no-e2e')

        try:
            with open(LOG_FILE, 'a') as log_file:
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_file,
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
        elif key == 9:  # TAB
            # Cycle through tabs.
            ids = [t[0] for t in TABS]
            current_tab = ids[(ids.index(current_tab) + 1) % len(ids)]
            log.lines.clear() if current_tab == 'logs' else None
        elif key in (ord('1'),):
            current_tab = 'dashboard'
        elif key in (ord('2'),):
            current_tab = 'models'
        elif key in (ord('3'),):
            current_tab = 'logs'
        elif key in (ord('4'),):
            current_tab = 'stats'
        elif key == curses.KEY_UP:
            log.scroll_up(3)
        elif key == curses.KEY_DOWN:
            log.scroll_down(3)
        elif key in (ord('?'),):
            stdscr.clear()
            stdscr.addstr(0, 1, "TUI v7 — Teclas", curses.color_pair(COLOR_PAIR_HEADER) | curses.A_BOLD)
            help_lines = [
                "  q / Q     salir",
                "  r / R     forzar refresh inmediato",
                "  p / P     pausar / reanudar auto-refresh",
                "  c / C     limpiar log mostrado",
                "  TAB       ciclar entre tabs (Dashboard/Models/Logs/Stats)",
                "  1..4      ir directo a una tab",
                "  l / L     cambiar layout (legacy)",
                "  m / M     configurar modelos por tier",
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
                log.append_msg(f'[TUI] deteniendo harness (PID {harness_pid})...')
                _stop_harness()
                harness_pid = 0
                cycle_launched = False
                log.append_msg('[TUI] harness detenido.')
            else:
                log.append_msg(f'[TUI] lanzando ciclo del harness ({cycle_duration}h '
                                 f'en /tmp/wt-night/apps/dashboard)...')
                pid = _launch_harness(hours=cycle_duration)
                if pid:
                    harness_pid = pid
                    cycle_launched = True
                    log.append_msg(f'[TUI] harness lanzado con PID {pid}, duración {cycle_duration}h')
                else:
                    log.append_msg('[TUI] ERROR: no se pudo lanzar el harness.')
            last_refresh = 0
        elif key in (ord('d'), ord('D')):
            # Menú de selección de duración.
            curses.curs_set(1)
            cycle_duration = _show_duration_menu(stdscr, cycle_duration)
            curses.curs_set(0)
            log.append_msg(f'[TUI] duración del ciclo: {cycle_duration}h '
                             f'{"(indefinido)" if cycle_duration == 0 else ""}')
            stdscr.clear()
            last_refresh = 0
        elif key in (ord('m'), ord('M')):
            # Configuración de modelos por tier.
            curses.curs_set(0)
            model_config = _show_model_config(stdscr, model_config)
            log.append_msg('[TUI] configuración de modelos actualizada. '
                             'Reinicia el harness para aplicar (tecla S).')
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
        # Tabs header.
        y = draw_tabs_header(stdscr, current_tab, y=y)
        # Render según tab activa.
        if current_tab == 'dashboard':
            # Schema v2: paneles nuevos Pipeline → Funnel → Nodes
            roles_available = bool(_safe_get_roles(last_status))
            if roles_available:
                if y < h - 4:
                    y = draw_pipeline_panel(stdscr, last_status, y)
                if y < h - 4:
                    y = draw_funnel_panel(stdscr, last_status, y)
                if y < h - 4:
                    y = draw_nodes_panel(stdscr, last_status, y)
            else:
                # Fallback schema v1: legacy process containers
                y = draw_process_containers(stdscr, last_status, y)
            if y < h - 12:
                y = draw_system_panel(stdscr, last_cpu, last_gpus, last_ollama, y)
        elif current_tab == 'models':
            # Cadencia + sistema (vista de modelos).
            cad_h = max(4, len(last_counts) + 2)
            y = draw_cadence_panel(stdscr, last_counts, y, cad_h)
            y = draw_system_panel(stdscr, last_cpu, last_gpus, last_ollama, y)
            if y < h - 4:
                y = draw_log_panel(stdscr, log, y, h - y - 2)
        elif current_tab == 'logs':
            # Log full screen.
            y = draw_log_panel(stdscr, log, y, h - y - 2)
        elif current_tab == 'stats':
            # Stats + log mini.
            y = draw_stats_panel(stdscr, last_status, y)
            if y < h - 4:
                y = draw_log_panel(stdscr, log, y, h - y - 2)
        # Footer.
        draw_footer(stdscr, paused, current_tab)
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
