# Operaciones — Multi-host Demeter v7

## Fase B: RTX 2060 como nodo local

La RTX 2060 ociosa se modela como un nodo local adicional, corriendo una 2ª instancia Ollama en puerto `:11435` (pineada a GPU1 via `CUDA_VISIBLE_DEVICES`).

### Setup

```bash
# Opcional: iniciar la 2ª instancia Ollama (pineada a GPU1)
bash scripts/start-2060-ollama.sh

# Verificar
curl -s http://127.0.0.1:11435/api/tags | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["models"]),"modelos")'
```

El harness detecta ambas instancias automaticamente (localhost + localhost-2060 en `LOOP_NODES`).

---

## Fase C: Multi-host (laptop remota)

### Exponer Ollama de la laptop (192.168.78.135)

#### Opción 1: Systemd override (persistente)

En la **laptop** (sudo):
```bash
sudo systemctl edit ollama.service

# Añade en [Service]:
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_ORIGINS=*"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
Environment="OLLAMA_KEEP_ALIVE=10m"

sudo systemctl daemon-reload
sudo systemctl restart ollama.service

# Verificar
sudo ss -tlnp | grep 11434    # debe ser 0.0.0.0:11434, NO 127.0.0.1
```

#### Opción 2: Túnel SSH (temporal, si OPS no disponible)

En el **host**:
```bash
ssh -fN -L 11534:127.0.0.1:11434 user@192.168.78.135
# Apunta a laptop via :11534 localhost
```

### Registrar nodo

En el **host contenedor**, exportar env:
```bash
export LOOP_NODES='localhost=http://host.docker.internal:11434,laptop=http://192.168.78.135:11434'
export LOOP_NODE_META='laptop:vram=8192:gpus=1:roles=hunter,filter:timeout_mult=2.0;localhost:roles=*'
```

O persistentemente en `.env.harness`:
```bash
LOOP_NODES=localhost=http://host.docker.internal:11434,laptop=http://192.168.78.135:11434
LOOP_NODE_META=laptop:vram=8192:gpus=1:roles=hunter,filter:timeout_mult=2.0;localhost:roles=*
```

### Verificar reachability

```bash
# Desde el contenedor:
curl -sS --max-time 3 http://192.168.78.135:11434/api/tags | head -c 100
curl -sS --max-time 3 http://192.168.78.135:11434/api/ps | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["models"]),"running")'

# Desde el host harness:
cat /tmp/harness-fleet.json | python3 -m json.tool | grep -A5 laptop
```

Debe mostrar `"alive": true` y latencia `<50ms`.

### Firewall (laptop)

```bash
# UFW (ubuntu)
sudo ufw allow from 192.168.78.0/24 to any port 11434 proto tcp

# firewall-cmd (rhel/fedora)
sudo firewall-cmd --add-port=11434/tcp --permanent
sudo firewall-cmd --reload
```

---

## Health-check automático

El harness corre un hilo demonio `start_health_loop(fleet, shutdown_event)` que:
- Re-prueba cada `~20s` (configurable `LOOP_NODE_HEALTH_INTERVAL`)
- Marca nodos `up`/`down` con histéresis (umbral de fallos)
- Escribe status en `/tmp/harness-fleet.json` (lee la TUI)
- NO bloquea pipeline: workers leen `up_nodes()` lock-free

Si un nodo cae:
- `fail_streak >= LOOP_NODE_FAIL_THRESHOLD` (default 2) → excluido temporalmente
- Se reintenta 1 vez por task (timeout acotado)
- No atascar la cola: timeout por-nodo (`LOOP_NODE_PROBE_TIMEOUT=3s`)

---

## Env vars de configuración

| Var | Default | Notas |
|---|---|---|
| `LOOP_NODES` | `localhost=http://host.docker.internal:11434` | `node_name=url,…` |
| `LOOP_NODE_META` | `localhost:roles=*` | `node:key=val;…` (roles, vram, timeout_mult) |
| `LOOP_NODE_PROBE_TIMEOUT` | `3` | Timeout [s] por probe |
| `LOOP_NODE_HEALTH_INTERVAL` | `20` | Interval [s] entre health-checks |
| `LOOP_NODE_FAIL_THRESHOLD` | `2` | Fallos antes de excluir |
| `LOOP_NODE_RECOVER_THRESHOLD` | `2` | Éxitos antes de re-incluir |

---

## Troubleshooting

### "Laptop marked down, never comes back"
- Verificar reachability: `curl -v http://192.168.78.135:11434/api/tags`
- Firewall: `sudo ufw allow from …` o `firewall-cmd …`
- OLLAMA_HOST debe ser `0.0.0.0`, no `127.0.0.1`
- SSH túnel: verificar `-L 11534:127.0.0.1:11434` y usar `:11534` en LOOP_NODES

### "RTX 2060 no recibe hunters"
- Verificar `:11435` arrancó: `curl http://127.0.0.1:11435/api/tags`
- Script idempotente: re-ejecutar `bash scripts/start-2060-ollama.sh`
- GPU1 UUID: `nvidia-smi --query-gpu=uuid --id=1`
- Logs: `/tmp/ollama-2060.log`

### "Hunters timeout en nodo remoto"
- Aumentar `LOOP_NODE_META` `timeout_mult=2.0` (para laptop: 2× timeout base)
- Revisar red latencia: `ping 192.168.78.135`
- Verificar modelos instalados: `curl http://192.168.78.135:11434/api/tags`
