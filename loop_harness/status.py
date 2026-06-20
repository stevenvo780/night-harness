"""Thread state and status-file builders for the harness/TUI contract."""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable


THREAD_STATES: dict[str, dict] = {}
THREAD_STATES_LOCK = threading.Lock()

ROLE_ORDER = ['hunter', 'filter', 'judge', 'fixer', 'cloud']
ROLE_LABELS = {
    'hunter': 'Hunters local',
    'filter': 'Filtros',
    'judge': 'Jueces',
    'fixer': 'Fixers',
    'cloud': 'Cloud hunters',
}


def update_thread_state(name: str, **kwargs) -> None:
    """Create or update one worker-thread state entry."""
    with THREAD_STATES_LOCK:
        if name not in THREAD_STATES:
            THREAD_STATES[name] = {
                'kind': '?',
                'role': None,
                'location': 'local',
                'node': 'local',
                'model': '?',
                'status': 'idle',
                'current_file': '',
                'current_task': '',
                'started_at': time.time(),
                'last_update': time.time(),
                'calls': 0,
                'errors': 0,
                'avg_ms': 0,
            }
        THREAD_STATES[name].update(kwargs)
        THREAD_STATES[name]['last_update'] = time.time()


def get_thread_states() -> list[dict]:
    """Return a copy of all worker states with derived age fields."""
    with THREAD_STATES_LOCK:
        out = []
        for name, state in THREAD_STATES.items():
            entry = dict(state)
            entry['name'] = name
            entry['age_sec'] = int(time.time() - state.get('started_at', time.time()))
            entry['idle_sec'] = int(time.time() - state.get('last_update', time.time()))
            out.append(entry)
        return out


def build_roles(
    *,
    stats: dict,
    stats_lock: threading.Lock,
    quarantined_models: Callable[[], list[str]],
    hunter_hilos: int,
    second_hilos: int,
    cloud_hunter_hilos: int,
    filter_hilos: int,
    judge_hilos: int,
    fixer_hilos: int,
) -> list[dict]:
    """Aggregate thread states into the schema-v2 roles array."""
    configured = {
        'hunter': hunter_hilos + second_hilos,
        'filter': filter_hilos,
        'judge': judge_hilos,
        'fixer': fixer_hilos,
        'cloud': cloud_hunter_hilos,
    }
    with stats_lock:
        started_at = stats['started_at']
    uptime = max(1, time.time() - started_at)
    role_data: dict[str, dict] = {}
    quarantined = set(quarantined_models())
    with THREAD_STATES_LOCK:
        states_snapshot = list(THREAD_STATES.items())

    for _name, state in states_snapshot:
        role = state.get('role') or state.get('kind', '?')
        if role in ('cloud', 'hunter'):
            role_key = 'cloud' if role == 'cloud' else 'hunter'
        elif role in ('filter', 'judge', 'fixer'):
            role_key = role
        else:
            role_key = role
        if role_key not in ROLE_ORDER:
            continue

        data = role_data.setdefault(role_key, {
            'live_n': 0,
            'busy_n': 0,
            'idle_n': 0,
            'done_n': 0,
            'calls_total': 0,
            'timeouts': 0,
            'errors': 0,
            'models': set(),
        })
        status = state.get('status', 'idle')
        if status == 'done':
            data['done_n'] += 1
        elif status == 'busy':
            data['live_n'] += 1
            data['busy_n'] += 1
        else:
            data['live_n'] += 1
            data['idle_n'] += 1
        data['calls_total'] += state.get('calls', 0)
        data['errors'] += state.get('errors', 0)
        model = state.get('model', '')
        if model and model != '?':
            data['models'].add(model)

    roles_out = []
    for role_key in ROLE_ORDER:
        data = role_data.get(role_key, {
            'live_n': 0,
            'busy_n': 0,
            'idle_n': 0,
            'done_n': 0,
            'calls_total': 0,
            'timeouts': 0,
            'errors': 0,
            'models': set(),
        })
        calls_total = data['calls_total']
        calls_per_min = round(calls_total / (uptime / 60.0), 2) if uptime > 0 else 0.0
        roles_out.append({
            'role': role_key,
            'label': ROLE_LABELS.get(role_key, role_key),
            'configured_n': configured.get(role_key, 0),
            'live_n': data['live_n'],
            'busy_n': data['busy_n'],
            'idle_n': data['idle_n'],
            'done_n': data['done_n'],
            'quarantined_n': sum(1 for model in data['models'] if model in quarantined),
            'calls_total': calls_total,
            'calls_per_min': calls_per_min,
            'timeouts': data['timeouts'],
            'errors': data['errors'],
            'models': sorted(data['models']),
        })
    return roles_out


def build_nodes(
    *,
    fleet_path: Path = Path('/tmp/harness-fleet.json'),
    ollama_base: str | None = None,
) -> list[dict]:
    """Build the schema-v2 nodes array from fleet state or local Ollama."""
    if fleet_path.exists():
        try:
            raw = json.loads(fleet_path.read_text())
            nodes_out = []
            for node in raw.get('nodes', []):
                gpus_out = []
                for gpu in node.get('gpus', []):
                    gpus_out.append({
                        'index': gpu.get('index', 0),
                        'name': gpu.get('name', '?'),
                        'vram_used_mib': (
                            gpu.get('vram_total_mib', 0) - gpu.get('vram_free_mib', 0)
                        ),
                        'vram_total_mib': gpu.get('vram_total_mib', 0),
                        'util_pct': gpu.get('util_pct', 0),
                        'loaded_models': (
                            [gpu['assigned_model']] if gpu.get('assigned_model') else []
                        ),
                    })
                ollama_models = []
                for model_name, vram_mib in node.get('running', {}).items():
                    ollama_models.append({
                        'name': model_name,
                        'vram_gb': round(vram_mib / 1024, 2),
                        'gpu_index': None,
                    })
                nodes_out.append({
                    'id': node.get('name', 'local'),
                    'host': node.get('base_url', '').replace('http://', '').replace('https://', ''),
                    'alive': node.get('online', False),
                    'latency_ms': 0,
                    'gpus': gpus_out,
                    'ollama_models': ollama_models,
                })
            return nodes_out
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    base = ollama_base or os.environ.get('LOOP_OLLAMA_BASE', 'http://host.docker.internal:11434')
    ollama_models = []
    latency_ms = 0
    alive = False
    try:
        t0 = time.time()
        with urllib.request.urlopen(f'{base}/api/ps', timeout=3) as response:
            latency_ms = int((time.time() - t0) * 1000)
            alive = True
            data = json.loads(response.read().decode('utf-8'))
        for model in data.get('models', []) or []:
            name = (model.get('name') or '').strip()
            size_vram = int(model.get('size_vram', 0) or 0)
            if name:
                ollama_models.append({
                    'name': name,
                    'vram_gb': round(size_vram / 1e9, 2),
                    'gpu_index': None,
                })
    except Exception:
        pass

    return [{
        'id': 'local',
        'host': base.replace('http://', '').replace('https://', ''),
        'alive': alive,
        'latency_ms': latency_ms,
        'gpus': [],
        'ollama_models': ollama_models,
    }]


def build_status_payload(
    *,
    pid: int,
    stats: dict,
    stats_lock: threading.Lock,
    orch_lock: threading.Lock,
    orch_prompts: list,
    e2e_journeys: list,
    orchestrator_model: str,
    roles: list[dict],
    nodes: list[dict],
    quota_snapshot: dict,
    memory_obj,
) -> dict:
    """Build the full schema-v2 status payload."""
    threads_v2 = []
    for state in get_thread_states():
        threads_v2.append({
            'name': state.get('name', '?'),
            'role': state.get('role') or state.get('kind', '?'),
            'location': state.get('location', 'local'),
            'node': state.get('node', 'local'),
            'model': state.get('model', '?'),
            'status': state.get('status', 'idle'),
            'current_file': state.get('current_file', ''),
            'current_task': state.get('current_task', ''),
            'calls': state.get('calls', 0),
            'errors': state.get('errors', 0),
            'avg_ms': state.get('avg_ms', 0),
            'age_sec': state.get('age_sec', 0),
            'idle_sec': state.get('idle_sec', 0),
        })

    with stats_lock:
        stats_snapshot = dict(stats)
    with orch_lock:
        prompts_snapshot = list(orch_prompts)
        journeys_snapshot = list(e2e_journeys)

    funnel = {
        'suspects_produced': stats_snapshot.get('suspects_produced', 0),
        'suspects_refuted': stats_snapshot.get('suspects_refuted_by_filter', 0),
        'suspects_known_fp': stats_snapshot.get('suspects_filtered_as_known_fp', 0),
        'bugs_confirmed': stats_snapshot.get('bugs_confirmed', 0),
        'fixes_attempted': stats_snapshot.get('fixes_attempted', 0),
        'fixes_succeeded': stats_snapshot.get('fixes_succeeded', 0),
        'fixes_reverted': stats_snapshot.get('fixes_reverted', 0),
        'fixes_to_review': stats_snapshot.get('fixes_to_review', 0),
        'e2e_findings': stats_snapshot.get('e2e_findings', 0),
        'e2e_devserver_skips': stats_snapshot.get('e2e_devserver_skips', 0),
        'opus_invocations': stats_snapshot.get('opus_invocations', 0),
        'rounds': stats_snapshot.get('rounds', 0),
        'clean_rounds_in_a_row': stats_snapshot.get('clean_rounds_in_a_row', 0),
    }
    return {
        'schema_version': 2,
        'ts': time.time(),
        'pid': pid,
        'uptime_sec': int(time.time() - stats_snapshot['started_at']),
        'funnel': funnel,
        'roles': roles,
        'threads': threads_v2,
        'orchestrator': {
            'model': orchestrator_model,
            'hunting_prompts_n': len(prompts_snapshot),
            'e2e_journeys_n': len(journeys_snapshot),
            'ready': bool(prompts_snapshot),
        },
        'nodes': nodes,
        'quota': quota_snapshot,
        'memory': {
            'total_rounds': memory_obj.total_rounds,
            'total_bugs_found': memory_obj.total_bugs_found,
            'total_fixes_applied': memory_obj.total_fixes_applied,
            'open_bugs': len(memory_obj.open_bugs()),
            'hardened_zones': sum(
                1 for zone in memory_obj.zones.values()
                if zone.clean_rounds >= 3
            ),
        },
        'stats': {
            'rounds': funnel['rounds'],
            'clean_rounds_in_a_row': funnel['clean_rounds_in_a_row'],
            'suspects_produced': stats_snapshot.get('suspects_produced', 0),
            'suspects_refuted_by_filter': stats_snapshot.get('suspects_refuted_by_filter', 0),
            'suspects_filtered_as_known_fp': stats_snapshot.get('suspects_filtered_as_known_fp', 0),
            'bugs_confirmed': funnel['bugs_confirmed'],
            'fixes_attempted': funnel['fixes_attempted'],
            'fixes_succeeded': funnel['fixes_succeeded'],
            'fixes_reverted': funnel['fixes_reverted'],
            'fixes_to_review': funnel['fixes_to_review'],
            'e2e_findings': funnel['e2e_findings'],
            'e2e_devserver_skips': funnel['e2e_devserver_skips'],
            'opus_invocations': funnel['opus_invocations'],
        },
    }


def write_status_file(status_file: Path, payload: dict) -> None:
    """Atomically write the status payload."""
    try:
        tmp = status_file.with_suffix('.tmp')
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        os.replace(tmp, status_file)
    except OSError:
        pass

