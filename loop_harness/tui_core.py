"""Pure data/system helpers used by the curses TUI."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections import Counter, deque


SCHEMA_VERSION = 2
LOG_LINES = 200
OLLAMA_URL = 'http://host.docker.internal:11434'


def node_host_label(node: dict) -> str:
    """Etiqueta de host robusta para el panel de nodos.

    Tolera `host` ausente o `null` (JSON null al arrancar, antes de resolver el
    host): cae a `id` y luego a '?'. Evita `None[:30]` que tumbaba el draw loop.
    """
    return str(node.get('host') or node.get('id') or '?')


class LogBuffer:
    """Circular in-memory buffer for recent log lines."""

    def __init__(self, max_lines: int = LOG_LINES):
        self.max = max_lines
        self.lines: deque[tuple[float, str]] = deque(maxlen=max_lines)
        self._cursor = 0
        self._last_size = 0

    def append_msg(self, msg: str) -> None:
        self.lines.append((time.time(), msg))

    def update(self, path: str) -> bool:
        """Append new bytes from a log file, handling rotation."""
        try:
            with open(path) as file:
                file.seek(0, 2)
                size = file.tell()
                if size < self._last_size:
                    self._last_size = 0
                    self.lines.clear()
                if size > self._last_size:
                    file.seek(self._last_size)
                    new = file.read()
                    self._last_size = size
                    added = 0
                    for line in new.splitlines():
                        match = re.match(r'\[(\d{2}):(\d{2}):(\d{2})\]', line)
                        if match:
                            now = time.localtime()
                            try:
                                ts = time.mktime((
                                    now.tm_year, now.tm_mon, now.tm_mday,
                                    int(match.group(1)),
                                    int(match.group(2)),
                                    int(match.group(3)),
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

    def scroll_up(self, n: int = 1) -> None:
        self._cursor = min(self._cursor + n, max(0, len(self.lines) - 1))

    def scroll_down(self, n: int = 1) -> None:
        self._cursor = max(0, self._cursor - n)

    def reset_cursor(self) -> None:
        self._cursor = 0


def model_family_short(model_id: str) -> str:
    """Return a compact display label for a model id."""
    known = (
        ('ollama:qwen3.5:9b', '9b'),
        ('ollama:qwen3-coder:30b', '30b'),
        ('ollama:qwen3-coder-next:cpu', '80b-cpu'),
        ('minimax/MiniMax-M3', 'mm-m3'),
        ('minimax/MiniMax-M2.5-highspeed', 'mm-m2.5hs'),
        ('minimax/MiniMax-M2.7-highspeed', 'mm-m2.7hs'),
        ('gemini:gemini-2.5-flash-lite', 'gem-fl'),
        ('gemini:gemini-2.5-pro', 'gem-pro'),
        ('gemini:gemini-3-flash-preview', 'gem-3fl'),
        ('gemini:gemini-3-pro-preview', 'gem-3pro'),
        ('claude:claude-sonnet-4-5', 'sonnet'),
        ('claude:claude-haiku-4-5', 'haiku'),
        ('codex:gpt-5.5', 'codex'),
        ('claude:claude-opus-4-6', 'opus'),
    )
    for prefix, label in known:
        if model_id.startswith(prefix):
            return label
    return model_id.split(':')[-1].split('/')[-1][:14]


def short_model(model_id: str) -> str:
    if ':' in model_id:
        return model_id.split(':', 1)[1]
    if '/' in model_id:
        return model_id.split('/')[-1]
    return model_id


def read_status(path: str, *, schema_version: int = SCHEMA_VERSION) -> dict:
    try:
        with open(path) as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError, FileNotFoundError):
        return {}
    sv = data.get('schema_version', 1)
    if sv != schema_version:
        data['_schema_warning'] = f'schema {sv} != expected {schema_version}'
    return data


def safe_int(val, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def read_harness_pid(path: str) -> int:
    try:
        with open(path, 'r') as file:
            content = file.read().strip()
            return int(content) if content else 0
    except (FileNotFoundError, ValueError, OSError):
        return 0


def safe_get_funnel(status: dict) -> dict:
    funnel = status.get('funnel')
    if isinstance(funnel, dict):
        return funnel
    stats = status.get('stats', {}) or {}
    return {
        'suspects_produced': safe_int(stats.get('suspects_produced', 0)),
        'suspects_refuted': safe_int(stats.get('suspects_refuted_by_filter', 0)),
        'suspects_known_fp': safe_int(stats.get('suspects_filtered_as_known_fp', 0)),
        'bugs_confirmed': safe_int(stats.get('bugs_confirmed', 0)),
        'fixes_attempted': safe_int(stats.get('fixes_attempted', 0)),
        'fixes_succeeded': safe_int(stats.get('fixes_succeeded', 0)),
        'fixes_reverted': safe_int(stats.get('fixes_reverted', 0)),
        'fixes_to_review': safe_int(stats.get('fixes_to_review', 0)),
        'e2e_findings': safe_int(stats.get('e2e_findings', 0)),
        'opus_invocations': safe_int(stats.get('opus_invocations', 0)),
        'rounds': safe_int(stats.get('rounds', 0)),
        'clean_rounds_in_a_row': safe_int(stats.get('clean_rounds_in_a_row', 0)),
    }


def safe_get_roles(status: dict) -> list:
    roles = status.get('roles')
    return roles if isinstance(roles, list) else []


def safe_get_nodes(status: dict) -> list:
    nodes = status.get('nodes')
    return nodes if isinstance(nodes, list) else []


def safe_get_threads(status: dict) -> list:
    threads = status.get('threads')
    return threads if isinstance(threads, list) else []


def read_log_calls(path: str, window_sec: int = 30) -> Counter:
    cutoff = time.time() - window_sec
    counts: Counter = Counter()
    try:
        with open(path) as file:
            file.seek(0, 2)
            size = file.tell()
            file.seek(max(0, size - 200_000), 0)
            for line in file:
                match = re.match(r'\[(\d{2}):(\d{2}):(\d{2})\].*\[call\] (\S+) ', line)
                if not match:
                    continue
                now = time.localtime()
                try:
                    ts = time.mktime((
                        now.tm_year,
                        now.tm_mon,
                        now.tm_mday,
                        int(match.group(1)),
                        int(match.group(2)),
                        int(match.group(3)),
                        0, 0, 0,
                    ))
                except (ValueError, OverflowError):
                    continue
                if ts < cutoff:
                    continue
                counts[match.group(4)] += 1
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
                match = re.search(r'(\d+\.\d+)\s*id', line)
                if match:
                    idle = match.group(1)
                    break
    except Exception:
        idle = '?'
    return {'load': load, 'idle_pct': idle}


def get_gpu_stats() -> list[dict]:
    out = []
    try:
        result = subprocess.run(
            [
                'nvidia-smi',
                '--query-gpu=index,name,memory.used,memory.total,utilization.gpu',
                '--format=csv,noheader',
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        for line in result.stdout.splitlines():
            parts = [part.strip() for part in line.split(', ')]
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


def get_ollama_models(ollama_url: str = OLLAMA_URL) -> list[dict]:
    out = []
    try:
        import urllib.request
        with urllib.request.urlopen(f'{ollama_url}/api/ps', timeout=2) as response:
            data = json.loads(response.read())
        for model in data.get('models', []):
            out.append({
                'name': model['name'],
                'vram_gb': model.get('size_vram', 0) / 1e9,
            })
    except Exception:
        pass
    return out


def check_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def default_model_config() -> dict:
    try:
        import tiers
        return {
            'tier0_local': [spec.id for spec in tiers.TIER0_HUNTERS],
            'tier0_cloud': [spec.id for spec in tiers.TIER0_CLOUD_HUNTERS],
            'tier1_filters': [spec.id for spec in tiers.TIER1_FILTERS],
            'tier2_judges': [spec.id for spec in tiers.TIER2_JUDGES],
            'tier3_fixers': [spec.id for spec in tiers.TIER3_FIXERS],
            'tier4_arbiters': [spec.id for spec in tiers.TIER4_ARBITERS],
        }
    except ImportError:
        return {
            'tier0_local': [],
            'tier0_cloud': [],
            'tier1_filters': [],
            'tier2_judges': [],
            'tier3_fixers': [],
            'tier4_arbiters': [],
        }


def load_model_config(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path) as file:
                return json.load(file)
        except (json.JSONDecodeError, OSError):
            pass
    return default_model_config()


def save_model_config(path: str, config: dict) -> None:
    try:
        with open(path, 'w') as file:
            json.dump(config, file, indent=2)
    except OSError:
        pass

