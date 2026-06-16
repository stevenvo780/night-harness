#!/usr/bin/env node
/**
 * e2e-runner — ejecutor de SONDAS de navegador para el harness nocturno.
 * Lee un escenario JSON (por stdin o archivo) y lo ejecuta con Playwright/chromium
 * (headless) contra el dev server local. Captura señales de BUG REAL de runtime:
 *   - errores de consola (console.error)
 *   - excepciones de página (pageerror, p.ej. React #419/#418)
 *   - respuestas HTTP con status >= 500
 *   - fallos de paso (selector no aparece, texto esperado ausente)
 * Devuelve JSON por stdout: { ok, consoleErrors, pageErrors, http5xx, stepFailures, url }
 *
 * Escenario: { baseURL, steps: [ {action, ...} ] }
 *   actions: goto{url} · click{selector} · fill{selector,value} · waitFor{selector,timeout}
 *            expectText{selector,text} · expectNoError{} (chequea acumulado)
 */
const fs = require('fs');

async function main() {
  const raw = process.argv[2] ? fs.readFileSync(process.argv[2], 'utf8') : fs.readFileSync(0, 'utf8');
  const scn = JSON.parse(raw);
  const baseURL = (scn.baseURL || 'http://localhost:3011').replace(/\/$/, '');

  let chromium;
  const NM = process.env.DASHBOARD_NM || '/tmp/demeter-loop/apps/dashboard/node_modules';
  try { ({ chromium } = require(NM + '/@playwright/test')); }
  catch { try { ({ chromium } = require(NM + '/playwright-core')); }
  catch { try { ({ chromium } = require(NM + '/playwright')); }
  catch { ({ chromium } = require('@playwright/test')); } } }

  const out = { ok: true, consoleErrors: [], pageErrors: [], http5xx: [], stepFailures: [], url: baseURL };
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  const ctx = await browser.newContext({ baseURL });
  const page = await ctx.newPage();

  page.on('console', (m) => { if (m.type() === 'error') out.consoleErrors.push(m.text().slice(0, 300)); });
  page.on('pageerror', (e) => out.pageErrors.push(String(e).slice(0, 300)));
  page.on('response', (r) => { if (r.status() >= 500) out.http5xx.push(`${r.status()} ${r.url().slice(0, 160)}`); });

  for (const step of (scn.steps || [])) {
    try {
      if (step.action === 'goto') {
        const resp = await page.goto(step.url, { waitUntil: 'networkidle', timeout: 30000 });
        if (resp && resp.status() >= 500) out.http5xx.push(`${resp.status()} ${step.url}`);
      } else if (step.action === 'click') {
        await page.click(step.selector, { timeout: step.timeout || 8000 });
      } else if (step.action === 'fill') {
        await page.fill(step.selector, String(step.value ?? ''), { timeout: step.timeout || 8000 });
      } else if (step.action === 'waitFor') {
        await page.waitForSelector(step.selector, { timeout: step.timeout || 8000 });
      } else if (step.action === 'expectText') {
        const txt = await page.textContent('body', { timeout: 8000 });
        if (!txt || !txt.includes(step.text)) out.stepFailures.push(`texto ausente: ${step.text}`);
      }
      await page.waitForTimeout(250);
    } catch (e) {
      out.stepFailures.push(`${step.action}(${step.selector || step.url || ''}): ${String(e).slice(0, 160)}`);
    }
  }

  out.ok = out.consoleErrors.length === 0 && out.pageErrors.length === 0 &&
           out.http5xx.length === 0 && out.stepFailures.length === 0;
  await browser.close();
  process.stdout.write(JSON.stringify(out));
}
main().catch((e) => { process.stdout.write(JSON.stringify({ ok: false, fatal: String(e) })); process.exit(0); });
