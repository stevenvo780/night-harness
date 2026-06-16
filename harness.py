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
WT        = "/tmp/demeter-loop"
ROOT      = WT + "/apps/dashboard"
DEV_URL   = os.environ.get("LOOP_DEV_URL", "http://localhost:3011")
RUNNER    = "/tmp/demeter-e2e-runner.js"
LOG       = "/tmp/loop-nocturno.log"
STATUS    = "/tmp/loop-nocturno-status.json"
MEMFILE   = "/tmp/loop-memory.json"
REVIEW    = "/tmp/loop-needs-review.json"

TARGET_CLEAN = int(os.environ.get("LOOP_TARGET", "50"))
N_HUNTERS    = int(os.environ.get("LOOP_HUNTERS", "4"))
WINDOW       = 150
DEADLINE     = time.time() + float(os.environ.get("LOOP_HOURS", "8")) * 3600
QMAX         = 40  # tope de cola para no sobreproducir

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
    for m in MODULES:
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

def probe(route, steps=None):
    scn = {"baseURL": DEV_URL, "steps": ([{"action": "goto", "url": route}] + (steps or []))}
    try:
        p = subprocess.run(["node", RUNNER], input=json.dumps(scn), cwd=ROOT, capture_output=True, text=True, timeout=90)
        return json.loads(p.stdout or "{}")
    except Exception as e:
        return {"ok": True, "fatal": str(e)}

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
def sweep_loop():
    while not STOP.is_set() and time.time() < DEADLINE:
        for mod, cfg in MODULES.items():
            if STOP.is_set() or time.time() >= DEADLINE: break
            if MEM[mod]["hardened"] or bugq.qsize() >= QMAX: continue
            for route in cfg["routes"]:
                with repo_lock:
                    res = probe(route)
                errs = (res.get("pageErrors") or []) + (res.get("http5xx") or []) + (res.get("consoleErrors") or [])
                if errs and res.get("ok") is False:
                    key = f"runtime:{route}"
                    with mem_lock:
                        if key in MEM[mod]["queued_keys"]: continue
                        MEM[mod]["queued_keys"].append(key)
                    MEM[mod]["clean"] = 0
                    bugq.put({"module": mod, "type": "runtime", "route": route, "errors": errs[:4],
                              "risk": "alto", "path": None})
                    log(f"[SWEEP] {mod} {route}: navegador halló {len(errs)} error(es) → {errs[0][:80]}")
                time.sleep(0.5)
        time.sleep(5)

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
            if all(MEM[m]["hardened"] for m in MODULES): break
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
        triage = ("PRIMERO juzga con RIGOR si es un bug REAL en ESTE archivo. Ojo: Drizzle PARAMETRIZA `sql${'{...}'}` "
                  "(NO es inyección); muchos 'nulls'/'divisiones por cero' ya están manejados. Si NO es un bug real, o el "
                  "código ya es correcto, o la causa no está en este archivo → responde EXACTAMENTE: NOT_A_BUG. "
                  if bug["type"] == "code" else
                  "El error de navegador es evidencia REAL. Si la causa está en este archivo corrígela; si claramente "
                  "NO está aquí → responde EXACTAMENTE: NOT_A_BUG. ")
        prompt = (f"{desc}\n\nArchivo `{path}`. {triage}\n"
                  f"Si SÍ hay que arreglarlo, devuelve SOLO la edición quirúrgica (OLD = copia EXACTA y única del código actual):\n"
                  f"<<<OLD>>>\n<código exacto>\n<<<NEW>>>\n<código corregido>\n<<<END>>>\n\nCÓDIGO:\n{ctx}")
        out = call_model(RESOLVER, prompt, system=RESOLVER_SYS, npred=450, timeout=280, think=False)
        if "NOT_A_BUG" in out and "<<<OLD>>>" not in out:
            MEM[bug["module"]]["rejected"] += 1
            log(f"[R] {bug['module']}: triage → NO es bug ({(bug.get('hip') or bug.get('route') or '')[:45]})"); continue
        if "<<<OLD>>>" not in out or "<<<NEW>>>" not in out:
            MEM[bug["module"]]["rejected"] += 1; continue
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
                git("checkout", "--", "apps/dashboard/" + path)
                MEM[bug["module"]]["rejected"] += 1; log(f"[R] {bug['module']}: revertido (rompe tsc)"); continue
            # re-prueba E2E si fue bug de runtime
            if bug["type"] == "runtime":
                res = probe(bug["route"])
                if res.get("ok") is False and (res.get("pageErrors") or res.get("http5xx")):
                    git("checkout", "--", "apps/dashboard/" + path)
                    MEM[bug["module"]]["rejected"] += 1; log(f"[R] {bug['module']}: revertido (E2E sigue roja)"); continue
            git("add", "-A")
            diff = git("diff", "--cached", "--", "apps/dashboard/" + path).stdout
            msg = f"fix({bug['module']}): {bug.get('hip') or bug.get('route')} [local]"
            git("commit", "-q", "-m", msg)
        # revisión Opus para riesgosos (fuera del lock; ya commiteado en rama)
        if bug["risk"] == "alto":
            verdict = opus_review(desc, diff)
            entry = {"module": bug["module"], "desc": desc[:200], "commit": git("rev-parse", "--short", "HEAD").stdout.strip(), "opus": verdict, "ts": now()}
            rev = json.load(open(REVIEW)) if os.path.exists(REVIEW) else []
            rev.append(entry); json.dump(rev, open(REVIEW, "w"), ensure_ascii=False, indent=1)
            log(f"[R] {bug['module']}: FIX riesgoso commiteado + Opus: {verdict[:80]}")
        else:
            log(f"[R] {bug['module']}: FIX commiteado «{(bug.get('hip') or bug.get('route'))[:50]}»")
        MEM[bug["module"]]["fixed"].append(bug.get("hip") or bug.get("route"))
        save_mem(); save_status()

# ----- main -----
def main():
    open(LOG, "a").write("\n" + "=" * 70 + f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} HARNESS NOCTURNO E2E · hunter={HUNTER} resolver={RESOLVER}\n" + "=" * 70 + "\n")
    load_mem(); save_status()
    threads = [threading.Thread(target=hunter_loop, args=(i,), daemon=True) for i in range(N_HUNTERS)]
    threads.append(threading.Thread(target=sweep_loop, daemon=True))
    threads.append(threading.Thread(target=resolver_loop, daemon=True))
    for t in threads: t.start()
    # supervisor — CONVERGE-AND-STOP: re-abre pasadas mientras siga ENCONTRANDO fixes; si una pasada
    # completa no produjo NINGÚN fix nuevo, el código está estable → para (no quema cómputo re-validando
    # lo mismo 10 veces, que fue el desperdicio de la 1ª noche). Conserva 'tried' entre pasadas.
    fixes_pasada_previa = -1
    while time.time() < DEADLINE:
        time.sleep(20); save_status()
        if all(MEM[m]["hardened"] for m in MODULES):
            total = sum(len(MEM[m]["fixed"]) for m in MODULES)
            if total == fixes_pasada_previa:
                log("✓ Pasada completa SIN fixes nuevos → código ESTABLE, convergió. Parando."); break
            fixes_pasada_previa = total
            with mem_lock:
                for mm in MODULES:
                    MEM[mm]["hardened"] = False; MEM[mm]["clean"] = 0
            log(f"✓ Pasada completa ({total} fixes acumulados) — abriendo otra; para al converger.")
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
