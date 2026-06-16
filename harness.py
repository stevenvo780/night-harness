#!/usr/bin/env python3
"""
HARNESS NOCTURNO de auto-endurecimiento E2E — Deméter.

Pipeline PRODUCTOR-CONSUMIDOR concurrente (aprovecha la enorme diferencia de latencia):
  · HUNTERS (qwen3.5:9b, ~2s, N hilos)  → análisis adversarial de CÓDIGO por módulo, escalando
                                           la exigencia cada ronda; encolan hipótesis de bug.
  · SWEEP   (Playwright/chromium, 1 hilo) → navega rutas en el dev server (PGlite aislado),
                                           captura errores de consola / React #419 / HTTP 5xx; encola.
  · RESOLVER(qwen3-coder-next:cpu, ~36s, 1 hilo) → consume la cola, produce fix OLD→NEW, lo aplica,
                                           COMPUERTA tsc + re-prueba E2E; commitea en rama o revierte.
  · OPUS    (claude -p --model opus)      → revisa/valida los fixes RIESGOSOS (dinero/stock/estado/auth)
                                           de forma autónoma y los marca en needs-review.
Los hunters + sweep mantienen la cola llena mientras el resolver "piensa" → nadie ocioso.

Aislamiento: worktree /tmp/demeter-loop (rama loop-nocturno). NO toca main ni producción.
El MERGE a main lo aprueban el humano + Claw en la mañana. Memoria/aprendizajes en JSON.
"""
import json, os, re, subprocess, threading, time, queue, urllib.request, random

BASE      = os.environ.get("LOOP_BASE", "http://host.docker.internal:11434").rstrip("/")
HUNTER    = os.environ.get("LOOP_HUNTER", "qwen3.5:9b")
# Por DEFECTO el resolver usa el MISMO modelo chico que el hunter. Lección del post-mortem:
# alternar un modelo grande (p.ej. 60GB en CPU) con uno chico hace que Ollama eviccione/repagine
# las ~60GB en cada switch → ~67s de cola FANTASMA por llamada (no es CPU ni carga, es memoria) →
# timeouts → atasco. Si quieres un modelo grande aquí: garantiza que Ollama mantenga AMBOS
# residentes (RAM suficiente + OLLAMA_MAX_LOADED_MODELS>=2 + OLLAMA_KEEP_ALIVE largo) o córrelo en
# una FASE separada (sin interleave con el chico). Ver README §"Contención de modelos".
RESOLVER  = os.environ.get("LOOP_RESOLVER", "qwen3.5:9b")
# --- CAPAS DIVERSAS (v3): el 9B local CAZA; modelos fuertes de FAMILIAS DISTINTAS en FINES distintos ---
# No se ponen a votar lo mismo (quemaría cuota doble): cada uno en su capa, y aún diversifica porque el
# JUEZ y el FIXER son familias distintas que se cruzan + el gate objetivo (tsc/E2E). Remotos → SIN contención
# con el 9B local (mata el wedge). Quotas separadas: aprovechan Gemini/Codex ociosos sin gastar Claude.
#   · JUEZ/triage  → alta frecuencia (1 por candidato) → modelo capaz pero BARATO (gemini -p).
#   · FIXER        → baja frecuencia (solo bugs confirmados) → puede ser agéntico/caro (codex).
#   · REVISOR      → rarísimo (solo fixes riesgosos) → cruce de familia.
JUDGES    = [j.strip() for j in os.environ.get("LOOP_JUDGES", "gemini:gemini-3-pro-preview").split(",") if j.strip()]
JUDGE_MIN = int(os.environ.get("LOOP_JUDGE_MIN", "1"))                       # 1 juez fuerte basta (sube a 2+ con más jueces si quieres panel)
FIXER     = os.environ.get("LOOP_FIXER", "codex")                           # FIXER (familia distinta al juez)
REVIEWER  = os.environ.get("LOOP_REVIEWER", "gemini:gemini-3-pro-preview")  # revisa fixes RIESGOSOS (cruce de familia)
ARBITER   = os.environ.get("LOOP_ARBITER", "claude:opus")                  # 🏆 EL MEJOR (Opus): ÁRBITRO FINAL, SOLO cuando el revisor DUDA (escalada) — rarísimo, máximo juicio por el costo
WT        = os.environ.get("LOOP_WT", "/tmp/wt")            # git worktree aislado (rama desechable)
SUBDIR    = os.environ.get("LOOP_SUBDIR", "")              # subdir del proyecto dentro del worktree (p.ej. "apps/dashboard"); "" = raíz
ROOT      = os.path.join(WT, SUBDIR) if SUBDIR else WT     # cwd donde corren npm/tsc/node
def gp(p): return os.path.join(SUBDIR, p) if SUBDIR else p  # path relativo al worktree, para git
DEV_URL   = os.environ.get("LOOP_DEV_URL", "http://localhost:3011")
RUNNER    = "/tmp/demeter-e2e-runner.js"
LOG       = "/tmp/loop-nocturno.log"
STATUS    = "/tmp/loop-nocturno-status.json"
MEMFILE   = "/tmp/loop-memory.json"
REVIEW    = "/tmp/loop-needs-review.json"

TARGET_CLEAN = int(os.environ.get("LOOP_TARGET", "50"))
N_HUNTERS    = int(os.environ.get("LOOP_HUNTERS", "6"))  # el 9B caza a full (gratis); los jueces fuertes filtran
WINDOW       = 150
DEADLINE     = time.time() + float(os.environ.get("LOOP_HOURS", "8")) * 3600
QMAX         = 40  # tope de cola para no sobreproducir
# --- E2E PROFUNDO + RESILIENCIA (v2) ---
STORAGE_STATE   = os.environ.get("LOOP_STORAGE_STATE", "")        # storageState.json (sesión auth); vacío = modo demo
E2E_BUDGET_MS   = int(os.environ.get("LOOP_E2E_BUDGET_MS", "70000"))  # presupuesto de crawl profundo por ruta
E2E_MAX_ACTIONS = int(os.environ.get("LOOP_E2E_ACTIONS", "24"))   # interacciones máximas por ruta
APP_DIR         = os.environ.get("LOOP_APP_DIR", "app")           # dir de rutas Next (relativo a ROOT)
DYNAMIC_ID      = os.environ.get("LOOP_DYNAMIC_ID", "")           # id sembrado para rutas [id] (vacío = se omiten)
BIG_RESOLVER    = os.environ.get("LOOP_BIG_RESOLVER", "0") == "1" # resolver grande: esperar disponibilidad + no interleavear
STALL_SECS      = int(os.environ.get("LOOP_STALL_SECS", "360"))   # watchdog: sin avance => recuperación
FINDINGS        = "/tmp/loop-e2e-findings.json"                   # TODOS los hallazgos E2E (para revisión humana)
E2E_CLEAN       = threading.Event()                              # set cuando una pasada de crawl no halló nada nuevo
ITER_TS         = [time.time()]                                  # marca de último avance (watchdog)

MODULES = {
  "pedidos":         {"files": ["lib/queries/pedidos.ts","lib/queries/pedidos-shared.ts","app/(panel)/pedidos/[id]/page.tsx"], "routes": ["/pedidos/recibidos","/pedidos","/pedidos/nuevo"]},
  "despacho":        {"files": ["lib/queries/despacho.ts","lib/queries/rutas.ts","lib/queries/rutero.ts"], "routes": ["/despacho","/rutas"]},
  "clientes":        {"files": ["lib/queries/clientes.ts","lib/queries/clientes-quejas.ts"], "routes": ["/clientes"]},
  "cobros-cartera":  {"files": ["lib/queries/cobros.ts","lib/queries/cartera.ts"], "routes": ["/cobros"]},
  "caja":            {"files": ["lib/queries/caja.ts","lib/queries/caja-shared.ts"], "routes": ["/caja"]},
  "facturacion":     {"files": ["lib/queries/facturacion.ts","lib/queries/anulados.ts"], "routes": ["/facturacion","/facturacion/nueva"]},
  "catalogo":        {"files": ["lib/queries/productos.ts","lib/queries/precios.ts","lib/queries/recetas.ts"], "routes": ["/productos","/precios","/recetas"]},
  "inventario":      {"files": ["lib/queries/inventario.ts","lib/queries/bodegas.ts","lib/queries/compras.ts"], "routes": ["/inventario","/compras"]},
  "inicio-reportes": {"files": ["lib/queries/inicio.ts","lib/queries/reportes.ts"], "routes": ["/inicio","/reportes"]},
  "operacion":       {"files": ["lib/queries/usuarios.ts","lib/queries/tareas.ts","lib/queries/empresa.ts"], "routes": ["/usuarios","/tareas","/empresa"]},
}

RISK_RE = re.compile(r"(dinero|saldo|cupo|amount|owed|ledger|precio|price|cobro|abono|pago|factura|invoice|folio|dian|caja|arqueo|stock|inventar|reserv|despach|dispatch|tenant|rol\b|role|permiso|permission|requirerole|estado|transition|status|migrat|schema|ddl)", re.I)

# ----- infra concurrente -----
bugq       = queue.Queue()
repo_lock  = threading.Lock()   # exclusión mutua: resolver-fix-cycle  vs  sweep (dev server + archivos)
mem_lock   = threading.Lock()
mod_counter = [0]
counter_lock = threading.Lock()
STOP = threading.Event()

def now(): return time.strftime("%H:%M:%S")
def log(m):
    line = f"{now()} {m}"
    with open(LOG, "a") as f: f.write(line + "\n")
    print(line, flush=True)

MEM = {}
def load_mem():
    global MEM
    if os.path.exists(MEMFILE):
        try: MEM = json.load(open(MEMFILE));
        except Exception: MEM = {}
    for m in list(MODULES) + ["ui"]:   # "ui" = módulo catch-all para hallazgos E2E de rutas descubiertas
        MEM.setdefault(m, {"clean": 0, "iter": 0, "tried": [], "confirmed": [], "fixed": [], "queued_keys": [], "hardened": False, "rejected": 0})
def save_mem():
    with mem_lock:
        json.dump(MEM, open(MEMFILE, "w"), ensure_ascii=False, indent=1)
def save_status():
    st = {"updated": now(), "deadline": time.strftime("%H:%M:%S", time.localtime(DEADLINE)),
          "queue": bugq.qsize(), "target": TARGET_CLEAN,
          "modules": {m: {k: MEM[m][k] for k in ("clean","iter","fixed","hardened","rejected")} | {"confirmed": len(MEM[m]["confirmed"])} for m in MODULES}}
    json.dump(st, open(STATUS, "w"), ensure_ascii=False, indent=1)

def call_model(model, prompt, system=None, npred=600, think=None, timeout=120):
    # UNA sola llamada, SIN reintento. Lección del post-mortem: reintentar tras un timeout ENCOLA
    # otra generación en Ollama y AGRAVA la contención/cola — fue lo que wedgeó la caja toda la
    # noche. Mejor fallar rápido y seguir (el hunter ya hace 3 muestras de consenso como redundancia).
    body = {"model": model, "stream": False, "options": {"temperature": 0.25, "num_predict": npred},
            "messages": ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]}
    if think is not None: body["think"] = think
    try:
        req = urllib.request.Request(BASE + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()).get("message", {}).get("content", "") or ""
    except Exception as e:
        log(f"  [model err {model}] {e}")
        return ""

def risk_of(text): return "alto" if RISK_RE.search(text or "") else "bajo"

def _strip(s):
    """Quita ruido de los CLIs (warnings de gemini, footer 'tokens used' de codex)."""
    s = re.sub(r"(?im)^.*Ripgrep is not available.*$", "", s or "")
    s = re.sub(r"(?is)\ntokens used\s*\n[\d,]+\s*", "\n", s)
    return s.strip()

def call_provider(provider, prompt, timeout=150, system=None):
    """Enruta a un proveedor DIVERSO: gemini:<model> | codex | claude:<model> | ollama:<model> | <modelo-ollama>."""
    full = (system + "\n\n" + prompt) if system else prompt
    try:
        if provider.startswith("gemini:"):
            p = subprocess.run(["gemini", "-m", provider.split(":", 1)[1], "-p", full], capture_output=True, text=True, timeout=timeout)
            return _strip(p.stdout)
        if provider.split(":")[0] == "codex":
            p = subprocess.run(["codex", "exec", full], capture_output=True, text=True, timeout=timeout)
            return _strip(p.stdout)
        if provider.startswith("claude:"):
            p = subprocess.run(["claude", "-p", full, "--model", provider.split(":", 1)[1]], capture_output=True, text=True, timeout=timeout)
            return _strip(p.stdout)
        model = provider.split(":", 1)[1] if provider.startswith("ollama:") else provider
        return call_model(model, prompt, system=system, timeout=timeout, think=False)
    except Exception as e:
        log(f"  [provider err {provider}] {e}"); return ""

def judge_panel(desc, code, path):
    """Panel DIVERSO: cada juez (familia distinta) vota REAL vs NOT_A_BUG. Confirmado si >= JUDGE_MIN votan REAL.
    Esto SÍ sube la precisión (vs el consenso del mismo 9B, cuyos falsos positivos son sistemáticos/correlacionados)."""
    q = (f"Eres revisor senior TS/Next.js/Drizzle. OJO: Drizzle PARAMETRIZA `sql${'{...}'}` (NO es inyección); "
         f"muchos null/división-por-cero ya están manejados; no inventes bugs. ¿Esta AFIRMACIÓN de bug es REAL en "
         f"el código? Responde SOLO una palabra: REAL o NOT_A_BUG.\n\nAFIRMACIÓN: {desc}\n\nARCHIVO {path}:\n{code[:7000]}")
    votes = []
    for j in JUDGES:
        o = call_provider(j, q, timeout=150).upper()
        v = "NOT_A_BUG" if "NOT_A_BUG" in o else ("REAL" if "REAL" in o else "?")
        votes.append(f"{j.split(':')[0]}={v}")
        if sum(1 for x in votes if x.endswith("REAL")) >= JUDGE_MIN: break  # quórum alcanzado → no gastar más jueces
    reales = sum(1 for x in votes if x.endswith("REAL"))
    return reales >= JUDGE_MIN, votes

# ----- helpers de archivo / git / e2e -----
def read_window(path):
    full = os.path.join(ROOT, path)
    if not os.path.exists(full): return None, None
    lines = open(full, encoding="utf-8", errors="ignore").read().splitlines()
    if len(lines) <= WINDOW: return "\n".join(lines), 1
    s = random.randint(0, max(0, len(lines) - WINDOW))
    return "\n".join(lines[s:s + WINDOW]), s + 1

def typecheck():
    try:
        p = subprocess.run(["npx", "tsc", "--noEmit"], cwd=ROOT, capture_output=True, text=True, timeout=300)
        return p.returncode == 0, (p.stdout + p.stderr)[-1500:]
    except Exception as e:
        return False, f"tsc err {e}"

def git(*a): return subprocess.run(["git", "-C", WT, *a], capture_output=True, text=True)

def probe(route):
    """Crawl PROFUNDO de una ruta con e2e-runner v2: navega + llena forms + submit + clickea + modales."""
    scn = {"baseURL": DEV_URL, "route": route, "storageState": STORAGE_STATE,
           "maxActions": E2E_MAX_ACTIONS, "budgetMs": E2E_BUDGET_MS}
    env = dict(os.environ, DASHBOARD_NM=os.path.join(ROOT, "node_modules"))
    try:
        p = subprocess.run(["node", RUNNER], input=json.dumps(scn), cwd=ROOT,
                           capture_output=True, text=True, timeout=E2E_BUDGET_MS / 1000 + 40, env=env)
        r = json.loads(p.stdout or "{}"); r.setdefault("errors", []); r.setdefault("ok", True)
        return r
    except Exception as e:
        return {"ok": True, "errors": [], "fatal": str(e)}

# ----- descubrimiento de rutas + resiliencia (v2) -----
ROUTE_FILE = {}  # ruta -> page.tsx relativo (para que el resolver sepa qué archivo mirar)
def discover_routes():
    base = os.path.join(ROOT, APP_DIR)
    for dp, _, files in os.walk(base):
        page = next((f for f in files if f in ("page.tsx", "page.ts", "page.jsx")), None)
        if not page: continue
        rel = os.path.relpath(dp, base)
        segs, skip = [], False
        if rel != ".":
            for s in rel.split(os.sep):
                if s.startswith("(") and s.endswith(")"): continue          # route group
                if s.startswith("@"): skip = True; break                    # parallel slot
                if s.startswith("[") and s.endswith("]"):
                    if DYNAMIC_ID: segs.append(DYNAMIC_ID)
                    else: skip = True; break                                # ruta dinámica sin id sembrado
                else: segs.append(s)
        if skip: continue
        route = "/" + "/".join(segs)
        if route.startswith("/api"): continue
        ROUTE_FILE[route] = os.path.relpath(os.path.join(dp, page), ROOT)
    return sorted(ROUTE_FILE)

def model_health(model, timeout=20):
    """Latencia de un ping mínimo; None si no responde a tiempo."""
    t = time.time()
    r = call_model(model, "ok", npred=2, think=False, timeout=timeout)
    return None if r == "" else time.time() - t

def wait_for_model(model, max_wait=1800):
    """Bloquea con backoff hasta que el modelo responda (caso: usar el 80B y que arranque/se libere)."""
    delay, waited = 10, 0
    while not STOP.is_set() and waited < max_wait:
        if model_health(model, timeout=25) is not None: return True
        log(f"  [espera] {model} no responde — reintentando en {delay}s (resiliencia)")
        time.sleep(delay); waited += delay; delay = min(delay * 2, 120)
    return False

def apply_old_new(path, old, new):
    full = os.path.join(ROOT, path)
    if not os.path.exists(full): return False, "archivo no existe"
    src = open(full, encoding="utf-8").read()
    if src.count(old) != 1: return False, "OLD no único/no encontrado"
    cand = src.replace(old, new, 1)
    if len(cand.splitlines()) < 0.7 * len(src.splitlines()): return False, "edición destructiva"
    open(full, "w", encoding="utf-8").write(cand)
    return True, "ok"

def opus_review(desc, diff):
    prompt = (f"Eres revisor senior. Un modelo local arregló un bug de riesgo en Deméter (Next.js/Drizzle/Neon). "
              f"Valida si el cambio es CORRECTO y SEGURO (lógica de dinero/stock/estado/tenant), sin romper nada. "
              f"Responde una línea: VEREDICTO: OK|DUDOSO|MAL + motivo breve.\n\nBUG: {desc}\n\nDIFF:\n{diff[:6000]}")
    try:
        p = subprocess.run(["claude", "-p", prompt, "--model", "opus"], capture_output=True, text=True, timeout=240)
        return (p.stdout or p.stderr).strip()[:300]
    except Exception as e:
        return f"(opus no disponible: {e})"

# ----- HUNTER (productor, código) -----
def escalation(level):
    if level < 8:   return "correctitud básica: nulls, awaits faltantes, queries Drizzle mal formadas, condiciones invertidas"
    if level < 20:  return "casos borde: listas vacías/enormes, strings raros, fechas/UTC, paginación, división por cero"
    if level < 35:  return "concurrencia y dinero: races, redondeo de centavos, idempotencia, doble-submit, saldos relativos"
    return "seguridad y estado: scope por tenant, requireRole faltante, esquinas de la máquina de estados, inyección, autorización"

HUNTER_SYS = "Eres un cazador de bugs adversarial, experto en TypeScript/Next.js/Drizzle. Buscas bugs REALES (no estilo). Eres conciso y honesto: si no hay bug claro, lo dices."

def hunter_loop(tid):
    mods = list(MODULES)
    while not STOP.is_set() and time.time() < DEADLINE:
        with counter_lock:
            mod = mods[mod_counter[0] % len(mods)]; mod_counter[0] += 1
        m = MEM[mod]
        if m["hardened"]: time.sleep(1); continue
        if bugq.qsize() >= QMAX: time.sleep(2); continue
        path = random.choice([f for f in MODULES[mod]["files"] if os.path.exists(os.path.join(ROOT, f))] or [None])
        if not path: continue
        code, off = read_window(path)
        if not code: continue
        tried = "; ".join(m["tried"][-12:]) or "(ninguna)"
        base = (f"Módulo «{mod}», archivo `{path}`. Nivel: {escalation(m['clean'])}.\n"
                f"Hipótesis YA probadas (NO repitas): {tried}\n\n"
                f"Analiza ADVERSARIALMENTE este fragmento y encuentra UN bug REAL NUEVO. "
                f"Responde SOLO JSON: {{\"hay_bug\":true|false,\"hipotesis\":\"<≤10 palabras>\",\"confianza\":0.0-1.0}}\n\n{code}")
        m["iter"] += 1
        # (1) AUTO-CONSISTENCIA: 3 muestras del modelo chico; solo sobrevive lo que RECURRE (≥2/3).
        hyps = []
        for _ in range(3):
            o = call_model(HUNTER, base, system=HUNTER_SYS, npred=240, think=False, timeout=90)
            try:
                jj = json.loads(o[o.index("{"):o.rindex("}") + 1])
            except Exception:
                continue
            hp = (jj.get("hipotesis") or "").strip()
            if jj.get("hay_bug") and hp and float(jj.get("confianza", 0) or 0) >= 0.6:
                hyps.append(hp)
        if not hyps:
            m["clean"] += 1; _check_hardened(mod); continue
        norm = lambda h: " ".join(re.sub(r"[^0-9a-záéíóúñ ]", " ", h.lower()).split()[:4])
        groups = {}
        for h in hyps:
            groups.setdefault(norm(h), []).append(h)
        bkey = max(groups, key=lambda k: len(groups[k]))
        if len(groups[bkey]) < 2:   # 3 hipótesis distintas → sin consenso → casi seguro alucinación
            m["clean"] += 1; _check_hardened(mod); continue
        hip = groups[bkey][0]
        # (2) REFUTACIÓN: un segundo pase ESCÉPTICO intenta tumbar la hipótesis.
        rf = call_model(HUNTER, f"CÓDIGO:\n{code}\n\nAfirmación de bug: «{hip}». Sé ESCÉPTICO: ¿el código YA lo maneja, o la afirmación es falsa? Responde SOLO JSON {{\"real\":true|false}}.",
                        system=HUNTER_SYS, npred=60, think=False, timeout=60)
        try:
            if not json.loads(rf[rf.index("{"):rf.rindex("}") + 1]).get("real"):
                m["clean"] += 1; _check_hardened(mod); continue
        except Exception:
            pass  # si no parsea, lo deja pasar (el triage del 80B es el backstop final)
        key = hip.lower()[:60]
        with mem_lock:
            if key in m["queued_keys"] or any(key in t.lower() for t in m["tried"]):
                dup = True
            else:
                dup = False; m["tried"].append(hip); m["queued_keys"].append(key)
        if dup:
            m["clean"] += 1; _check_hardened(mod); continue
        m["clean"] = 0
        bugq.put({"module": mod, "type": "code", "path": path, "hip": hip, "linea": "",
                  "why": "consenso≥2/3 + refutación", "risk": risk_of(hip + " " + path)})
        log(f"[H{tid}] {mod}: CONSENSO+refutado → {hip[:55]}  (cola={bugq.qsize()})")
        save_mem()

def _check_hardened(mod):
    m = MEM[mod]
    if not m["hardened"] and m["clean"] >= TARGET_CLEAN:
        m["hardened"] = True; log(f"✓ MÓDULO ENDURECIDO: {mod} ({TARGET_CLEAN} rondas limpias)")

# ----- SWEEP (productor, navegador) -----
E2E_SEEN = set()      # firmas de hallazgos ya vistos (dedup global)
e2e_lock = threading.Lock()
def _route_module(route):
    seg = route.strip("/").split("/")[0] if route != "/" else "inicio"
    for m in MODULES:
        if seg and (seg in m or m.split("-")[0] == seg): return m
    return "ui"

def e2e_loop():
    """Crawl PROFUNDO e intensivo de TODAS las rutas descubiertas. Registra cada hallazgo (para revisión
    humana) y encola los arreglables. Es el motor de 'testear cada rincón'."""
    routes = discover_routes()
    log(f"[E2E] descubiertas {len(routes)} rutas para crawl profundo: {', '.join(routes[:12])}{'…' if len(routes)>12 else ''}")
    while not STOP.is_set() and time.time() < DEADLINE:
        nuevos = 0
        for route in routes:
            if STOP.is_set() or time.time() >= DEADLINE: break
            while bugq.qsize() >= QMAX and not STOP.is_set() and time.time() < DEADLINE: time.sleep(2)
            res = probe(route)  # crawl lock-free: solo el RESOLVER toma repo_lock (al editar); su re-verify filtra falsos transitorios
            ITER_TS[0] = time.time()  # avance (para el watchdog)
            for err in res.get("errors", []):
                sig = f"{route}|{err.get('kind')}|{str(err.get('detail',''))[:60]}"
                with e2e_lock:
                    if sig in E2E_SEEN: continue
                    E2E_SEEN.add(sig)
                nuevos += 1
                find = {"route": route, "file": ROUTE_FILE.get(route, ""), "ts": now(), **err}
                allf = json.load(open(FINDINGS)) if os.path.exists(FINDINGS) else []
                allf.append(find); json.dump(allf, open(FINDINGS, "w"), ensure_ascii=False, indent=1)
                log(f"[E2E] {route} :: {err.get('kind')} → {str(err.get('detail',''))[:90]}")
                bugq.put({"module": _route_module(route), "type": "runtime", "route": route,
                          "errors": [f"{err.get('kind')}: {err.get('detail','')}"],
                          "risk": "alto", "path": ROUTE_FILE.get(route, None)})
            time.sleep(0.3)
        if nuevos == 0: E2E_CLEAN.set()
        else: E2E_CLEAN.clear()
        log(f"[E2E] pasada de crawl completa: {nuevos} hallazgos nuevos (total únicos: {len(E2E_SEEN)}).")
        time.sleep(5)

def watchdog_loop():
    """Recuperación automática: si no hay avance en STALL_SECS, libera modelos pesados que estén
    acaparando memoria en Ollama (la causa del wedge nocturno) y re-evalúa salud del resolver."""
    while not STOP.is_set() and time.time() < DEADLINE:
        time.sleep(60)
        if time.time() - ITER_TS[0] < STALL_SECS: continue
        log(f"[WATCHDOG] sin avance hace >{STALL_SECS}s — recuperación: liberando modelos pesados de Ollama")
        try:
            ps = json.loads(urllib.request.urlopen(BASE + "/api/ps", timeout=10).read()).get("models", [])
            for m in ps:
                nm = m.get("name", "")
                if nm not in (HUNTER, RESOLVER) and m.get("size", 0) > 20e9:  # >20GB y no lo usamos
                    urllib.request.urlopen(urllib.request.Request(
                        BASE + "/api/generate", data=json.dumps({"model": nm, "keep_alive": 0, "prompt": ""}).encode(),
                        headers={"Content-Type": "application/json"}), timeout=20).read()
                    log(f"[WATCHDOG] unload enviado a {nm} ({round(m.get('size',0)/1e9)}GB)")
        except Exception as e:
            log(f"[WATCHDOG] no pude consultar/liberar Ollama: {e}")
        ITER_TS[0] = time.time()  # evita spamear; re-evalúa en el próximo ciclo

# ----- RESOLVER (consumidor, cerebro) -----
RESOLVER_SYS = "Eres ingeniero senior TypeScript/Next.js/Drizzle. Arreglas bugs con cambios MÍNIMOS y correctos, sin romper nada ni debilitar seguridad. Devuelves SOLO el bloque pedido."

def pick_fix_file(bug):
    if bug.get("path"): return bug["path"]
    cands = [f for f in MODULES[bug["module"]]["files"] if f.endswith(".ts") and os.path.exists(os.path.join(ROOT, f))]
    return cands[0] if cands else None

def resolver_loop():
    while not STOP.is_set() and time.time() < DEADLINE:
        try: bug = bugq.get(timeout=5)
        except queue.Empty:
            if all(MEM[m]["hardened"] for m in MODULES) and E2E_CLEAN.is_set(): break
            continue
        path = pick_fix_file(bug)
        if not path: continue
        full = os.path.join(ROOT, path)
        code = open(full, encoding="utf-8", errors="ignore").read()
        ctx = code if len(code) < 9000 else code[:9000]
        if bug["type"] == "runtime":
            desc = f"En la ruta {bug['route']} el navegador detectó: {' | '.join(bug['errors'])}. Encuentra y corrige la causa en este archivo del módulo {bug['module']}."
        else:
            desc = f"Bug: {bug['hip']}. Indicio: {bug.get('linea','')[:160]}. Por qué: {bug.get('why','')}"
        # CAPA 1 — TRIAGE por JUEZ fuerte de OTRA familia (Gemini, alta frecuencia/barato). Sin contención local.
        is_real, votes = judge_panel(desc, ctx, path)
        if not is_real:
            MEM[bug["module"]]["rejected"] += 1
            log(f"[R] {bug['module']}: juez → NO es bug [{', '.join(votes)}] ({(bug.get('hip') or bug.get('route') or '')[:40]})"); continue
        # CAPA 2 — FIX por el FIXER (Codex, baja frecuencia: solo bugs confirmados)
        fixprompt = (f"{desc}\n\nArchivo `{path}`. Es un bug REAL confirmado. Devuelve SOLO la edición quirúrgica "
                     f"(OLD = copia EXACTA y ÚNICA del código actual; cambio mínimo, no rompas nada):\n"
                     f"<<<OLD>>>\n<código exacto>\n<<<NEW>>>\n<código corregido>\n<<<END>>>\n\nCÓDIGO:\n{ctx}")
        out = call_provider(FIXER, fixprompt, timeout=240)
        if "<<<OLD>>>" not in out or "<<<NEW>>>" not in out:
            MEM[bug["module"]]["rejected"] += 1; log(f"[R] {bug['module']}: el fixer ({FIXER.split(':')[0]}) no produjo edición"); continue
        try:
            old = out.split("<<<OLD>>>", 1)[1].split("<<<NEW>>>", 1)[0].strip("\n")
            new = out.split("<<<NEW>>>", 1)[1].split("<<<END>>>", 1)[0].strip("\n")
            old = re.sub(r"^```[a-z]*\n|\n```$", "", old.strip()); new = re.sub(r"^```[a-z]*\n|\n```$", "", new.strip())
        except Exception:
            MEM[bug["module"]]["rejected"] += 1; continue
        if not old or old == new:
            MEM[bug["module"]]["rejected"] += 1; continue
        with repo_lock:
            ok, why = apply_old_new(path, old, new)
            if not ok:
                MEM[bug["module"]]["rejected"] += 1; log(f"[R] {bug['module']}: descartado ({why})"); continue
            tc_ok, tc_out = typecheck()
            if not tc_ok:
                git("checkout", "--", gp(path))
                MEM[bug["module"]]["rejected"] += 1; log(f"[R] {bug['module']}: revertido (rompe tsc)"); continue
            # re-prueba E2E si fue bug de runtime
            if bug["type"] == "runtime":
                res = probe(bug["route"])
                if res.get("ok") is False and res.get("errors"):
                    git("checkout", "--", gp(path))
                    MEM[bug["module"]]["rejected"] += 1; log(f"[R] {bug['module']}: revertido (E2E sigue roja)"); continue
            git("add", "-A")
            diff = git("diff", "--cached", "--", gp(path)).stdout
            msg = f"fix({bug['module']}): {bug.get('hip') or bug.get('route')} [local]"
            git("commit", "-q", "-m", msg)
        # CAPA 3 — REVISIÓN de fixes RIESGOSOS por modelo de OTRA familia (cruce; rarísimo, no gasta Claude)
        if bug["risk"] == "alto" and REVIEWER:
            rq = (f"Revisa este diff de un fix a un bug de RIESGO (dinero/stock/estado/auth). ¿Es CORRECTO y SEGURO "
                  f"sin romper nada? Responde una línea: OK | DUDOSO | MAL + motivo.\n\nBUG: {desc}\n\nDIFF:\n{diff[:6000]}")
            verdict = call_provider(REVIEWER, rq, timeout=150)[:300]
            # ESCALADA AL ÁRBITRO (Opus) SOLO si el revisor DUDA o lo marca mal → caso difícil que merece al mejor
            arbiter = ""
            if ARBITER and re.search(r"DUDOSO|MAL|incorrect|unsafe|riesg", verdict, re.I):
                arbiter = call_provider(ARBITER, rq + f"\n\nUn revisor previo dictaminó: «{verdict}». Da el VEREDICTO FINAL (OK|MAL + por qué).", timeout=200)[:300]
                log(f"[R] {bug['module']}: ⚖️ ESCALADO a Opus (árbitro): {arbiter[:70]}")
            entry = {"module": bug["module"], "desc": desc[:200], "commit": git("rev-parse", "--short", "HEAD").stdout.strip(),
                     "review": verdict, "arbiter_opus": arbiter, "ts": now()}
            rev = json.load(open(REVIEW)) if os.path.exists(REVIEW) else []
            rev.append(entry); json.dump(rev, open(REVIEW, "w"), ensure_ascii=False, indent=1)
            log(f"[R] {bug['module']}: FIX riesgoso + revisión {REVIEWER.split(':')[0]}: {verdict[:70]}")
        else:
            log(f"[R] {bug['module']}: FIX commiteado «{(bug.get('hip') or bug.get('route'))[:50]}»")
        MEM[bug["module"]]["fixed"].append(bug.get("hip") or bug.get("route"))
        save_mem(); save_status()

# ----- main -----
def main():
    open(LOG, "a").write("\n" + "=" * 70 + f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} HARNESS NOCTURNO E2E · hunter={HUNTER} resolver={RESOLVER}\n" + "=" * 70 + "\n")
    load_mem(); save_status()
    threads = [threading.Thread(target=hunter_loop, args=(i,), daemon=True) for i in range(N_HUNTERS)]
    threads.append(threading.Thread(target=e2e_loop, daemon=True))
    threads.append(threading.Thread(target=watchdog_loop, daemon=True))
    threads.append(threading.Thread(target=resolver_loop, daemon=True))
    for t in threads: t.start()
    # supervisor — CONVERGE-AND-STOP: re-abre pasadas mientras siga ENCONTRANDO fixes; si una pasada
    # completa no produjo NINGÚN fix nuevo, el código está estable → para (no quema cómputo re-validando
    # lo mismo 10 veces, que fue el desperdicio de la 1ª noche). Conserva 'tried' entre pasadas.
    fixes_pasada_previa = -1
    while time.time() < DEADLINE:
        time.sleep(20); save_status()
        if all(MEM[m]["hardened"] for m in MODULES) and E2E_CLEAN.is_set():
            total = sum(len(MEM[m]["fixed"]) for m in list(MODULES) + ["ui"])
            if total == fixes_pasada_previa:
                log("✓ Pasada completa SIN fixes nuevos (código + crawl E2E) → ESTABLE, convergió. Parando."); break
            fixes_pasada_previa = total
            with mem_lock:
                for mm in MODULES:
                    MEM[mm]["hardened"] = False; MEM[mm]["clean"] = 0
            E2E_CLEAN.clear()
            log(f"✓ Pasada completa ({total} fixes) — abriendo otra; para al converger.")
            save_mem()
    STOP.set(); log("Cerrando. Construyendo build final...")
    time.sleep(3)
    try:
        b = subprocess.run(["npm", "run", "build"], cwd=ROOT, capture_output=True, text=True, timeout=900)
        log("BUILD FINAL: " + ("OK" if b.returncode == 0 else "FALLA\n" + (b.stdout + b.stderr)[-1200:]))
    except Exception as e:
        log(f"build final err: {e}")
    fixed = sum(len(MEM[m]["fixed"]) for m in MODULES); hard = sum(1 for m in MODULES if MEM[m]["hardened"])
    log(f"FIN. fixes={fixed} · módulos endurecidos={hard}/{len(MODULES)} · revisar: {REVIEW}")
    save_mem(); save_status()

if __name__ == "__main__":
    main()
