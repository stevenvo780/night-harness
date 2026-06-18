#!/usr/bin/env python3
import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from tiers import ModelSpec

HEADROOM = 0.80
HEALTH_INTERVAL_SECS = 20.0
FLEET_STATUS_PATH = '/tmp/harness-fleet.json'
LOCAL_TOKENS = ('localhost', '127.0.0.1', 'host.docker.internal')

# Threshold: GPUs with VRAM < this are "small" and only get SMALL_HUNTER_PREF models.
# RTX 2060 = 6144 MiB, RTX 5070 Ti = 16303 MiB. 8000 MiB splits them cleanly.
SMALL_GPU_VRAM_THRESHOLD_MIB = 8000

BIG_HUNTER_PREF = ('qwen3-coder:30b', 'devstral:24b', 'qwen3:14b')
# Small GPU: 3b primero para que la 2060 NO compita con la 5070 por el 7b.
SMALL_HUNTER_PREF = ('qwen2.5-coder:3b', 'qwen2.5:3b', 'gemma3:4b', 'qwen2.5-coder:7b')
# Fallback big-GPU: si la 5070 Ti no tiene 30b/24b/14b, baja a 7b.
# Evita que GPUs potentes queden ociosas en máquinas sin el 30b instalado.
BIG_GPU_FALLBACK_PREF = ('qwen2.5-coder:7b', 'qwen2.5-coder:3b', 'gemma3:4b')
# Hunters que puede correr un nodo CPU-only (laptop, server sin GPU).
# El primero que esté instalado en el nodo gana. Diseñado para exprimir
# al máximo la RAM disponible sin caer a swap.
# Orden: de MAYOR a menor consumo de RAM — el fleet toma el primero disponible
# y NO comprueba si cabe en la RAM del nodo (lo hace ollama al cargar).
# El usuario debe asegurar que el modelo elegido cabe en su RAM.
#   llama3.1:70b-q4 ≈ 40GB  →  requiere nodo con ≥44GB RAM
#   qwen3:32b ≈ 20GB        →  ≥24GB
#   gpt-oss:20b ≈ 12GB      →  ≥16GB
#   nemotron-3-nano:30b ≈ 22GB → ≥26GB (RECOMENDADO para laptops 32-42GB)
#   devstral:24b ≈ 14GB     →  ≥18GB
#   qwen2.5-coder:7b ≈ 5GB  →  ≥8GB (fallback universal)
CPU_HUNTER_PREF = ('nemotron-3-nano:30b', 'qwen3:32b', 'gpt-oss:20b',
                   'devstral:24b', 'qwen2.5-coder:7b', 'llama3.1:70b-instruct-q4_K_M')
CPU_REASONER_PREF = ('qwen3-coder-next:q4_K_M', 'qwen3-coder-next:cpu', 'qwen3-coder:30b')

# VRAM estimation: spec §4.4 — est_vram_mib(size_bytes) = size_bytes / MiB * 1.10
VRAM_OVERHEAD = 1.10


def est_vram_mib(size_bytes: int) -> int:
    """Estima VRAM (MiB) de un modelo a partir de su tamaño en disco (bytes).
    Formula del spec §4.4: size_bytes / (1024*1024) * 1.10 (overhead VRAM).
    """
    return int(size_bytes / (1024 * 1024) * VRAM_OVERHEAD)


@dataclass
class Gpu:
    index: int
    name: str
    vram_total_mib: int
    vram_free_mib: int
    util_pct: int
    uuid: str
    assigned_model: Optional[str] = None
    assigned_role: Optional[str] = None


@dataclass
class CpuSlot:
    cores: int = 0
    ram_free_mib: int = 0
    assigned_model: Optional[str] = None
    assigned_role: Optional[str] = None


@dataclass
class Node:
    name: str
    base_url: str
    online: bool
    gpus: list[Gpu]
    cpu: Optional[CpuSlot] = None
    models: list[str] = field(default_factory=list)
    running: dict[str, int] = field(default_factory=dict)
    is_local: bool = False


def discover_local_gpus() -> list:
    try:
        result = subprocess.run(
            [
                'nvidia-smi',
                '--query-gpu=index,name,memory.used,memory.total,utilization.gpu,uuid',
                '--format=csv,noheader,nounits',
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logging.debug(f'fleet: nvidia-smi failed: {e}')
        return []
    if result.returncode != 0:
        return []
    out: list = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(',')]
        if len(parts) < 6:
            continue
        try:
            idx = int(parts[0])
            name = parts[1]
            used = int(parts[2])
            total = int(parts[3])
            util = int(parts[4])
            uuid = parts[5]
        except ValueError:
            continue
        if total <= 0:
            continue
        out.append(Gpu(
            index=idx,
            name=name,
            vram_total_mib=total,
            vram_free_mib=max(0, total - used),
            util_pct=util,
            uuid=uuid,
        ))
    return out


def discover_models(base_url: str) -> list:
    try:
        with urllib.request.urlopen(f'{base_url}/api/tags', timeout=5) as r:
            data = json.loads(r.read().decode('utf-8'))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError, ValueError) as e:
        logging.debug(f'fleet: /api/tags failed for {base_url!r}: {e}')
        return []
    out = []
    for m in data.get('models', []) or []:
        name = (m.get('name') or '').strip()
        if not name:
            continue
        if 'embed' in name.lower():
            continue
        out.append(name)
    return sorted(list(dict.fromkeys(out)))


def discover_running(base_url: str) -> dict:
    try:
        with urllib.request.urlopen(f'{base_url}/api/ps', timeout=5) as r:
            data = json.loads(r.read().decode('utf-8'))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError, ValueError) as e:
        logging.debug(f'fleet: /api/ps failed for {base_url!r}: {e}')
        return {}
    out: dict = {}
    for m in data.get('models', []) or []:
        name = (m.get('name') or '').strip()
        if not name:
            continue
        size_vram = max(0, int(m.get('size_vram', 0) or 0))
        out[name] = size_vram // (1024 * 1024)
    return out


def _is_local(base_url: str) -> bool:
    return any(tok in base_url for tok in LOCAL_TOKENS)


def discover_node(name: str, base_url: str) -> Node:
    is_local = _is_local(base_url)
    gpus = discover_local_gpus() if is_local else []
    models = discover_models(base_url)
    running = discover_running(base_url)
    online = bool(models) or bool(running)
    cpu: Optional[CpuSlot] = None
    if is_local:
        try:
            cpu = CpuSlot(cores=os.cpu_count() or 0, ram_free_mib=0)
        except Exception as e:
            logging.debug(f'fleet: cpu detection failed: {e}')
            cpu = None
    return Node(
        name=name,
        base_url=base_url,
        online=online,
        gpus=gpus,
        cpu=cpu,
        models=models,
        running=running,
        is_local=is_local,
    )


def _make_ollama_spec(node: Node, model_name: str, gpu: Optional[Gpu], role: str) -> ModelSpec:
    if gpu is not None:
        num_ctx = 8192 if gpu.vram_total_mib < SMALL_GPU_VRAM_THRESHOLD_MIB else 32768
    else:
        num_ctx = 32768
    spec = ModelSpec(
        id=f'ollama:{model_name}',
        family='local_qwen',
        role='local',
        relative_cost=2,
        context_window=num_ctx,
        strengths=('code', 'reasoning') if role == 'fixer' else ('code', 'fast'),
    )
    object.__setattr__(spec, 'base_url', node.base_url)
    object.__setattr__(spec, 'gpu_index', gpu.index if gpu is not None else None)
    object.__setattr__(spec, 'gpu_uuid', gpu.uuid if gpu is not None else None)
    object.__setattr__(spec, 'node', node.name)
    object.__setattr__(spec, 'num_ctx', num_ctx)
    return spec


def _is_big_gpu(gpu: Gpu) -> bool:
    """True si la GPU tiene suficiente VRAM para modelos grandes (30B/24B/14B).
    Threshold: >= SMALL_GPU_VRAM_THRESHOLD_MIB (8000 MiB). La RTX 2060 (6144 MiB)
    cae en 'small'; la RTX 5070 Ti (16303 MiB) cae en 'big'.
    """
    return gpu.vram_total_mib >= SMALL_GPU_VRAM_THRESHOLD_MIB


def plan_models(node: Node) -> dict:
    """Asigna modelos a GPUs segun VRAM disponible y preferencias del spec §4.4.

    Reglas clave:
    - GPU big (>= 8000 MiB): fixer + big hunters (30B/24B/14B).
    - GPU small (< 8000 MiB): solo small hunters (7b/3b); NO asigna modelos grandes.
    - rank=0 (GPU mas grande): candidato a fixer si 30B disponible.
    - rank>=1 (GPUs menores) O GPUs small: solo SMALL_HUNTER_PREF.
    """
    # Sin GPUs NO retornamos temprano: un nodo CPU-only (p.ej. remoto) todavía
    # puede servir un reasoner (el loop de GPUs simplemente no asigna nada).
    installed = set(node.models)
    gpus_sorted = sorted(node.gpus, key=lambda g: -g.vram_total_mib)

    hunter_specs: list = []
    fixer_spec: Optional[ModelSpec] = None
    reasoner_specs: list = []

    # HEADROOM (0.80) se aplicará en el futuro cuando se implemente VRAM budget checking
    for rank, gpu in enumerate(gpus_sorted):
        if _is_big_gpu(gpu) and rank == 0:
            # Big GPU, primera. Para EVITAR thrashing/OOM (el 30B ≈12.3GB ya casi
            # llena los 16GB), NO apilamos varios modelos grandes en la misma GPU:
            # el 30B sirve como fixer Y como hunter. Si no hay 30B, un único big
            # hunter que quepa. Si tampoco hay 14b/24b, FALLBACK a 7b/3b para
            # que la GPU no quede ociosa.
            fixer_model = 'qwen3-coder:30b' if 'qwen3-coder:30b' in installed else None
            if fixer_model is not None:
                fixer_spec = _make_ollama_spec(node, fixer_model, gpu, 'fixer')
                gpu.assigned_model = fixer_model
                gpu.assigned_role = 'hunter_fixer'
                hunter_specs.append(_make_ollama_spec(node, fixer_model, gpu, 'hunter'))
            else:
                assigned = False
                for pref in BIG_HUNTER_PREF:
                    if pref in installed:
                        hunter_specs.append(_make_ollama_spec(node, pref, gpu, 'hunter'))
                        gpu.assigned_model = pref
                        gpu.assigned_role = 'hunter'
                        assigned = True
                        break
                # Fallback big GPU → small hunter (evita GPU potente ociosa)
                if not assigned:
                    for pref in BIG_GPU_FALLBACK_PREF:
                        if pref in installed:
                            hunter_specs.append(_make_ollama_spec(node, pref, gpu, 'hunter'))
                            gpu.assigned_model = pref
                            gpu.assigned_role = 'hunter'
                            break
        else:
            # Small GPU o GPUs adicionales (rank >= 1) → solo small hunters
            # Esto garantiza que un 14b no se asigne a la RTX 2060 (6144 MiB).
            for pref in SMALL_HUNTER_PREF:
                if pref in installed:
                    hunter_specs.append(_make_ollama_spec(node, pref, gpu, 'hunter'))
                    if gpu.assigned_model is None:
                        gpu.assigned_model = pref
                        gpu.assigned_role = 'hunter'
                    break

    # CPU slot: para nodos locales, `node.cpu` es CpuSlot(); para nodos
    # remotos (laptop, server) sin info de CPU local, lo tratamos como
    # CPU-only con CpuSlot() vacío para poder asignar hunters.
    cpu_slot = node.cpu if node.cpu is not None else CpuSlot()

    # 1) Reasoner: si hay modelo grande CPU, lo asignamos como reasoner.
    reasoner_assigned = False
    for pref in CPU_REASONER_PREF:
        if pref in installed:
            spec = _make_ollama_spec(node, pref, None, 'reasoner')
            reasoner_specs.append(spec)
            cpu_slot.assigned_model = pref
            cpu_slot.assigned_role = 'reasoner'
            reasoner_assigned = True
            break
    # Si es nodo local, persistir el cpu_slot.
    if node.cpu is not None:
        node.cpu = cpu_slot

    # Budget fill: exprimir VRAM libre metiendo modelos EXTRA en la misma GPU.
    # El primer modelo ya se asignó arriba; ahora, si sobra VRAM, llenamos
    # con cuantos modelos chicos adicionales quepan (sin duplicarlos entre GPUs).
    # Cada modelo extra = otro hunter en paralelo = más abundancia.
    _FILL_VRAM_EST: dict[str, int] = {
        '3b': 2200, '4b': 3700, '7b': 5500, '9b': 7000,
        '12b': 8000, '14b': 9000, '20b': 13000, '24b': 15000,
        '30b': 13000, '32b': 20000,
    }
    assigned_models = {g.assigned_model for g in gpus_sorted if g.assigned_model}
    assigned_models.update(
        spec.id.rsplit(':', 1)[-1] for spec in hunter_specs if spec.id.startswith('ollama:')
    )
    # VRAM budget: 80% del total de la GPU, menos lo ya asignado.
    gpu_budget: dict[int, int] = {}
    for gpu in gpus_sorted:
        est_assigned = next(
            (v for k, v in _FILL_VRAM_EST.items() if gpu.assigned_model and k in gpu.assigned_model),
            4000,
        )
        gpu_budget[gpu.index] = int(gpu.vram_total_mib * 0.85) - est_assigned
    for gpu in gpus_sorted:
        for pref in SMALL_HUNTER_PREF + BIG_GPU_FALLBACK_PREF:
            base = pref.rsplit(':', 1)[-1] if ':' in pref else pref
            if pref in installed and pref not in assigned_models:
                est = next(
                    (v for k, v in _FILL_VRAM_EST.items() if k in base),
                    4000,
                )
                if gpu_budget.get(gpu.index, 0) > est:
                    hunter_specs.append(_make_ollama_spec(node, pref, gpu, 'hunter'))
                    gpu_budget[gpu.index] -= est
                    assigned_models.add(pref)

    # 2) CPU hunter: si el nodo NO tiene GPUs (laptop/server remoto CPU-only),
    # o si tiene GPUs pero NO reasoner y el modelo CPU es distinto al GPU,
    # podemos paralelizar CPU+GPU en el mismo nodo.
    if not gpus_sorted and not reasoner_assigned:
        # Nodo CPU-only (laptop): primer modelo en CPU_HUNTER_PREF que esté.
        for pref in CPU_HUNTER_PREF:
            if pref in installed:
                spec = _make_ollama_spec(node, pref, None, 'hunter')
                hunter_specs.append(spec)
                cpu_slot.assigned_model = pref
                cpu_slot.assigned_role = 'hunter'
                break
    elif gpus_sorted and not reasoner_assigned:
        # Nodo con GPUs: añade hunter CPU si es DISTINTO al GPU ya asignado.
        for pref in CPU_HUNTER_PREF:
            if pref in installed and pref not in (g.assigned_model for g in gpus_sorted):
                spec = _make_ollama_spec(node, pref, None, 'hunter')
                hunter_specs.append(spec)
                if cpu_slot.assigned_model is None:
                    cpu_slot.assigned_model = pref
                    cpu_slot.assigned_role = 'hunter'
                break

    return {'hunter': hunter_specs, 'fixer': fixer_spec, 'reasoner': reasoner_specs}


class Fleet:
    def __init__(self, nodes: list):
        self.nodes: list = list(nodes)
        self._hunter_specs: list = []
        self._fixer_spec: Optional[ModelSpec] = None
        self._reasoner_specs: list = []
        for node in self.nodes:
            plan = plan_models(node)
            self._hunter_specs.extend(plan['hunter'])
            if self._fixer_spec is None and plan['fixer'] is not None:
                self._fixer_spec = plan['fixer']
            self._reasoner_specs.extend(plan['reasoner'])

    def hunter_specs(self) -> list:
        return list(self._hunter_specs)

    def fixer_spec(self) -> Optional[ModelSpec]:
        return self._fixer_spec

    def reasoner_specs(self) -> list:
        return list(self._reasoner_specs)

    def all_specs(self) -> list:
        out: list = list(self._hunter_specs)
        if self._fixer_spec is not None:
            out.append(self._fixer_spec)
        out.extend(self._reasoner_specs)
        return out

    def status(self) -> list:
        out: list = []
        for n in self.nodes:
            entry = {
                'name': n.name,
                'base_url': n.base_url,
                'online': n.online,
                'is_local': n.is_local,
                'gpus': [{
                    'index': g.index,
                    'name': g.name,
                    'vram_total_mib': g.vram_total_mib,
                    'vram_free_mib': g.vram_free_mib,
                    'util_pct': g.util_pct,
                    'uuid': g.uuid,
                    'assigned_model': g.assigned_model,
                    'assigned_role': g.assigned_role,
                } for g in n.gpus],
                'models': list(n.models),
                'running': {k: int(v) for k, v in n.running.items()},
            }
            if n.cpu is not None:
                entry['cpu'] = {
                    'cores': n.cpu.cores,
                    'ram_free_mib': n.cpu.ram_free_mib,
                    'assigned_model': n.cpu.assigned_model,
                    'assigned_role': n.cpu.assigned_role,
                }
            else:
                entry['cpu'] = None
            out.append(entry)
        return out


def _parse_loop_nodes(raw: str) -> list:
    out: list = []
    for entry in raw.split(','):
        entry = entry.strip()
        if not entry or '=' not in entry:
            continue
        name, base_url = entry.split('=', 1)
        name = name.strip()
        base_url = base_url.strip()
        if not name or not base_url:
            continue
        out.append((name, base_url))
    return out


def build_fleet() -> Fleet:
    default_base = os.environ.get('LOOP_OLLAMA_BASE', 'http://host.docker.internal:11434')
    raw = os.environ.get('LOOP_NODES') or f'localhost={default_base}'
    nodes: list = []
    for name, base_url in _parse_loop_nodes(raw):
        try:
            node = discover_node(name, base_url)
        except Exception as e:
            logging.warning(f'fleet: failed to discover node {name!r} ({base_url!r}): {e}')
            node = Node(name=name, base_url=base_url, online=False, gpus=[], is_local=_is_local(base_url))
        nodes.append(node)
    return Fleet(nodes)


def _spec_info(spec: Optional[ModelSpec]) -> Optional[dict]:
    if spec is None:
        return None
    return {
        'id': spec.id,
        'family': spec.family,
        'role': spec.role,
        'node': getattr(spec, 'node', None),
        'base_url': getattr(spec, 'base_url', None),
        'gpu_index': getattr(spec, 'gpu_index', None),
        'gpu_uuid': getattr(spec, 'gpu_uuid', None),
        'num_ctx': getattr(spec, 'num_ctx', None),
    }


def write_fleet_status(fleet: Fleet, path: str = FLEET_STATUS_PATH) -> None:
    payload = {
        'ts': time.time(),
        'schema_version': 1,
        'nodes': fleet.status(),
        'hunter_specs': [_spec_info(s) for s in fleet.hunter_specs()],
        'fixer_spec': _spec_info(fleet.fixer_spec()),
        'reasoner_specs': [_spec_info(s) for s in fleet.reasoner_specs()],
    }
    tmp_path = f'{path}.tmp'
    try:
        with open(tmp_path, 'w') as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, path)
    except OSError as e:
        logging.warning(f'fleet: write_fleet_status failed for {path!r}: {e}')


def _health_loop_body(fleet: Fleet, shutdown_event: threading.Event) -> None:
    while not shutdown_event.is_set():
        try:
            for node in list(fleet.nodes):
                try:
                    node.models = discover_models(node.base_url)
                    node.running = discover_running(node.base_url)
                    node.online = bool(node.models) or bool(node.running)
                    if node.is_local:
                        node.gpus = discover_local_gpus()
                except Exception as e:
                    logging.warning(f'fleet: health probe failed for {node.name!r}: {e}')
                    node.online = False
            write_fleet_status(fleet)
        except Exception as e:
            logging.warning(f'fleet: health loop iteration failed: {e}')
        shutdown_event.wait(HEALTH_INTERVAL_SECS)


def start_health_loop(fleet: Fleet, shutdown_event) -> threading.Thread:
    t = threading.Thread(
        target=_health_loop_body,
        args=(fleet, shutdown_event),
        daemon=True,
        name='fleet-health-loop',
    )
    t.start()
    return t


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [fleet] %(message)s')
    fleet = build_fleet()
    print(json.dumps(fleet.status(), indent=2))
    print('hunter:', [s.id for s in fleet.hunter_specs()])
    print('fixer:', fleet.fixer_spec().id if fleet.fixer_spec() else None)
    print('reasoner:', [s.id for s in fleet.reasoner_specs()])
    print('all:', [s.id for s in fleet.all_specs()])
