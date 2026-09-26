#!/usr/bin/env node
// Шаг 3: в уже открытом DevTools выбираем вкладку Resources Saver и нажимаем
// «Save All Resources» (#up-save) — настоящая кнопка расширения.
import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';

const OUT = path.resolve(process.env.OUTPUT_DIR || './artifacts');
const PORT = parseInt(process.env.CDP_PORT || '9222', 10);
const PANEL_TIMEOUT = parseInt(process.env.PANEL_TIMEOUT_MS || '40000', 10);
const ZIP_TIMEOUT = parseInt(process.env.ZIP_TIMEOUT_MS || '180000', 10);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(...a);

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${PORT}`);

  let devtools = null;
  const deadline = Date.now() + PANEL_TIMEOUT;
  while (Date.now() < deadline && !devtools) {
    for (const c of browser.contexts()) {
      for (const p of c.pages()) {
        if (p.url().startsWith('devtools://')) { devtools = p; break; }
      }
      if (devtools) break;
    }
    if (!devtools) await sleep(1000);
  }
  if (!devtools) throw new Error('окно DevTools не найдено среди целей Chrome');
  log(`DevTools: ${devtools.url().slice(0, 90)}`);

  // вкладка Resources Saver в панели DevTools
  let panel = null;
  while (Date.now() < deadline && !panel) {
    for (const f of devtools.frames()) {
      if (f.url().startsWith('chrome-extension://') && f.url().includes('content.html')) panel = f;
    }
    if (!panel) {
      const tab = devtools.locator(
        'li[aria-label*="Resources Saver"], [aria-label*="Resources Saver"], [title*="Resources Saver"]'
      ).first();
      if (await tab.count().catch(() => 0)) {
        await tab.click({ timeout: 3000 }).catch(() => {});
        log('   клик по вкладке Resources Saver');
      }
      await sleep(1500);
    }
  }
  if (!panel) throw new Error('панель Resources Saver не появилась в DevTools');
  log(`панель: ${panel.url()}`);

  const btn = panel.locator('#up-save');
  await btn.waitFor({ state: 'visible', timeout: 20000 });
  log('нажимаю «Save All Resources»…');
  await panel.evaluate(() => document.getElementById('up-save').click());

  const zipDeadline = Date.now() + ZIP_TIMEOUT;
  let zip = null;
  while (Date.now() < zipDeadline) {
    const z = fs.readdirSync(OUT).filter((f) => f.endsWith('.zip') && !f.endsWith('.crdownload'));
    if (z.length) { zip = z[z.length - 1]; break; }
    await sleep(2000);
  }
  if (!zip) {
    const state = await panel.textContent('body').catch(() => '');
    log(`состояние панели: ${(state || '').replace(/\s+/g, ' ').slice(0, 300)}`);
    throw new Error('ZIP не появился');
  }
  log(`готово: ${path.join(OUT, zip)} (${(fs.statSync(path.join(OUT, zip)).size / 1048576).toFixed(1)} МБ)`);
  await browser.close().catch(() => {});
}

main().catch((e) => { console.error('ошибка:', e.message); process.exit(1); });
