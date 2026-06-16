#!/usr/bin/env node
/**
 * e2e-runner v2 — CRAWLER PROFUNDO E INTENSIVO para el harness nocturno.
 *
 * No solo navega: EJERCITA cada rincón de una ruta — llena formularios y hace submit, clickea
 * botones, abre diálogos/modales, cambia selects — capturando TODA señal de bug real: errores de
 * consola, excepciones de página (React #419/#418), respuestas HTTP >= 400, requests fallidas, y la
 * aparición de UI de error tras una acción.
 *
 * Corre headless contra un dev server con DATOS SEMBRADOS y (opcional) sesión autenticada
 * (storageState). Pensado para una DB de TEST aislada — las mutaciones (incluidos deletes) son
 * seguras y deseables: así se prueban los flujos de verdad.
 *
 * Entrada (JSON por stdin o archivo): { baseURL, route, storageState?, maxActions?, budgetMs? }
 * Salida (JSON por stdout): { route, ok, errors:[{kind,where,detail}], actions:[...], counts }
 */
const fs = require('fs');

function loadChromium() {
  const NM = process.env.DASHBOARD_NM || (process.cwd() + '/node_modules');
  for (const p of [NM + '/@playwright/test', NM + '/playwright-core', NM + '/playwright', '@playwright/test']) {
    try { return require(p).chromium; } catch { /* next */ }
  }
  throw new Error('No encuentro playwright/chromium');
}

const VALUE_FOR = (type, name = '') => {
  const n = name.toLowerCase();
  if (/email/.test(type) || /email|correo/.test(n)) return 'test+e2e@example.com';
  if (/number/.test(type) || /cantidad|monto|valor|precio|qty|amount|cupo|tax/.test(n)) return '1';
  if (/date/.test(type) || /fecha/.test(n)) return new Date().toISOString().slice(0, 10);
  if (/tel/.test(type) || /tel|phone|celular|movil/.test(n)) return '3001234567';
  if (/password/.test(type)) return 'Test1234!';
  if (/search|buscar/.test(n)) return 'a';
  return 'Prueba E2E';
};

async function main() {
  const raw = process.argv[2] ? fs.readFileSync(process.argv[2], 'utf8') : fs.readFileSync(0, 'utf8');
  const scn = JSON.parse(raw);
  const baseURL = (scn.baseURL || 'http://localhost:3011').replace(/\/$/, '');
  const route = scn.route || '/';
  const maxActions = scn.maxActions ?? 24;
  const budgetMs = scn.budgetMs ?? 70000;
  const deadline = Date.now() + budgetMs;

  const chromium = loadChromium();
  const out = { route, ok: true, errors: [], actions: [], counts: {} };
  const pushErr = (kind, where, detail) => out.errors.push({ kind, where, detail: String(detail).slice(0, 280) });

  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  const ctx = await browser.newContext({
    baseURL,
    ...(scn.storageState && fs.existsSync(scn.storageState) ? { storageState: scn.storageState } : {}),
  });
  const page = await ctx.newPage();

  let curWhere = route;
  page.on('console', (m) => { if (m.type() === 'error') pushErr('console', curWhere, m.text()); });
  page.on('pageerror', (e) => pushErr('pageerror', curWhere, e));
  page.on('requestfailed', (r) => { const f = r.failure(); if (f && !/ERR_ABORTED/.test(f.errorText)) pushErr('requestfailed', curWhere, `${r.method()} ${r.url().slice(0, 110)} :: ${f.errorText}`); });
  page.on('response', (r) => { if (r.status() >= 400) pushErr('http', curWhere, `${r.status()} ${r.request().method()} ${r.url().slice(0, 110)}`); });
  page.on('dialog', (d) => d.accept().catch(() => {}));

  async function checkErrorUI(when) {
    try {
      const txt = (await page.textContent('body', { timeout: 4000 })) || '';
      if (/Algo salió mal|Application error|Internal Server Error|Unhandled Runtime Error|client-side exception|something went wrong/i.test(txt)) {
        pushErr('errorUI', `${curWhere} (${when})`, txt.replace(/\s+/g, ' ').slice(0, 200));
      }
    } catch { /* ignore */ }
  }
  async function gotoRoute() {
    curWhere = `goto ${route}`;
    try {
      const resp = await page.goto(route, { waitUntil: 'networkidle', timeout: 30000 });
      if (resp && resp.status() >= 400) pushErr('http', curWhere, `${resp.status()} ${route}`);
    } catch (e) { pushErr('console', curWhere, `goto fallo: ${e}`); }
    await checkErrorUI('tras navegar');
  }

  await gotoRoute();

  let actions = 0;
  const tryAction = async (label, fn) => {
    if (actions >= maxActions || Date.now() > deadline) return;
    actions++; curWhere = `${route} » ${label}`; out.actions.push(label);
    try { await fn(); } catch (e) { pushErr('console', curWhere, `acción "${label}": ${e}`); }
    await checkErrorUI(label);
  };

  // 1) Selects → opción no-default (revela estados dependientes)
  const selects = await page.$$('select');
  for (let i = 0; i < selects.length && actions < maxActions; i++) {
    await tryAction(`select[${i}]`, async () => {
      const opts = await selects[i].$$('option');
      if (opts.length > 1) await selects[i].selectOption({ index: 1 }).catch(() => {});
    });
  }
  // 2) Formularios → llenar + submit (en DB de test es seguro)
  const forms = await page.$$('form');
  for (let i = 0; i < forms.length && actions < maxActions; i++) {
    await tryAction(`form[${i}].fill+submit`, async () => {
      const inputs = await forms[i].$$('input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=file]), textarea');
      for (const inp of inputs.slice(0, 8)) {
        const type = (await inp.getAttribute('type')) || 'text';
        const name = (await inp.getAttribute('name')) || (await inp.getAttribute('placeholder')) || '';
        await inp.fill(VALUE_FOR(type, name), { timeout: 3000 }).catch(() => {});
      }
      const submit = await forms[i].$('button[type=submit], button:not([type]), input[type=submit]');
      if (submit) { await submit.click({ timeout: 4000 }).catch(() => {}); await page.waitForTimeout(700); }
    });
  }
  // 3) Botones (abrir modales/acciones); si abren form, llenarlo y enviarlo
  const btns = await page.$$('button:not([type=submit])');
  for (let i = 0; i < btns.length && actions < maxActions; i++) {
    if (!(await btns[i].isVisible().catch(() => false))) continue;
    const label = ((await btns[i].innerText().catch(() => '')) || `button[${i}]`).trim().slice(0, 30);
    await tryAction(`click "${label}"`, async () => {
      await btns[i].click({ timeout: 4000 }).catch(() => {});
      await page.waitForTimeout(500);
      const dlgForm = await page.$('[role=dialog] form, .modal form');
      if (dlgForm) {
        const inputs = await dlgForm.$$('input:not([type=hidden]), textarea');
        for (const inp of inputs.slice(0, 6)) {
          const t = (await inp.getAttribute('type')) || 'text';
          const nm = (await inp.getAttribute('name')) || '';
          await inp.fill(VALUE_FOR(t, nm), { timeout: 2500 }).catch(() => {});
        }
        const s = await dlgForm.$('button[type=submit], button:not([type])');
        if (s) { await s.click({ timeout: 3000 }).catch(() => {}); await page.waitForTimeout(700); }
      }
      if (!page.url().includes(route.split('?')[0])) await gotoRoute();
    });
  }

  out.ok = out.errors.length === 0;
  out.counts = out.errors.reduce((a, e) => { a[e.kind] = (a[e.kind] || 0) + 1; return a; }, { actions });
  await browser.close();
  process.stdout.write(JSON.stringify(out));
}
main().catch((e) => { process.stdout.write(JSON.stringify({ ok: false, fatal: String(e) })); process.exit(0); });
