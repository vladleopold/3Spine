#!/usr/bin/env node
// Открываем Chrome с расширением Resources-Saver и нажимаем в нём
// «Save All Resources» (кнопка #up-save в панели DevTools расширения).
//
// Важно: панель расширения живёт внутри DevTools, а popup.html — только
// инструкция. Поэтому Chrome запускается с --auto-open-devtools-for-tabs,
// затем мы подключаемся к DevTools по CDP, находим панель Resources Saver
// и жмём кнопку.
import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';

const URL_ = process.env.URL || 'https://example.com';
const OUT = path.resolve(process.env.OUTPUT_DIR || './rs-out');
const EXT = path.resolve(process.env.EXT_DIR || './.chrome-ext');
const PROFILE = path.resolve(process.env.PROFILE || path.join(OUT, '.profile'));
const CHROME = process.env.CHROME_PATH
  || (fs.existsSync('/usr/bin/google-chrome') ? '/usr/bin/google-chrome' : '');
const PORT = parseInt(process.env.CDP_PORT || '9222', 10);
const WAIT = parseInt(process.env.WAIT_MS || '20000', 10);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(...a);

async function cdpUp(port) {
  for (let i = 0; i * 500 < 20000; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (r.ok) return true;
    } catch { /* ещё не поднялся */ }
    await sleep(500);
  }
  return false;
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  fs.mkdirSync(PROFILE, { recursive: true });
  const m = JSON.parse(fs.readFileSync(path.join(EXT, 'manifest.json'), 'utf8'));
  log(`ссылка:    ${URL_}`);
  log(`расширение: ${m.name} v${m.version} из ${EXT}`);
  log(`chrome:    ${CHROME || 'системный'}`);

  const { spawn } = await import('child_process');
  const args = [
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${PROFILE}`,
    '--no-sandbox', '--disable-dev-shm-usage', '--no-first-run', '--no-default-browser-check',
    '--disable-blink-features=AutomationControlled',
    '--window-size=1400,900',
    // DevTools открывается сам — в нём и живёт панель расширения
    '--auto-open-devtools-for-tabs',
    `--disable-extensions-except=${EXT}`,
    `--load-extension=${EXT}`,
  ];
  if (process.env.DISPLAY) args.unshift('--start-maximized');
  const child = spawn(CHROME || 'google-chrome', args, {
    stdio: 'ignore', detached: true, env: { ...process.env },
  });
  log(`Chrome запущен (pid ${child.pid}), жду DevTools…`);

  try {
    if (!await cdpUp(PORT)) throw new Error('Chrome не открыл порт отладки');
    const browser = await chromium.connectOverCDP(`http://127.0.0.1:${PORT}`);

    // 1) открываем ссылку и ждём её загрузки
    const ctx = browser.contexts()[0];
    const page = ctx.pages()[0] || await ctx.newPage();
    await page.goto(URL_, { waitUntil: 'domcontentloaded', timeout: 45000 });
    log(`страница открыта: ${page.url()}`);
    log(`заголовок: ${(await page.title().catch(() => '')) || '—'}`);
    const cdp = await ctx.newCDPSession(page);
    await cdp.send('Browser.setDownloadBehavior',
      { behavior: 'allow', downloadPath: OUT, eventsEnabled: true });
    await sleep(Math.min(WAIT, 8000));

    // 2) находим окно DevTools и панель Resources Saver
    let panel = null, devtools = null;
    for (let i = 0; i * 500 < 20000 && !panel; i++) {
      for (const c of browser.contexts()) {
        for (const p of c.pages()) {
          if (!p.url().startsWith('devtools://')) continue;
          devtools = p;
          for (const f of p.frames()) {
            if (f.url().startsWith('chrome-extension://') && f.url().includes('content.html')) panel = f;
          }
          if (!panel) {
            // панель ещё не выбрана — кликаем вкладку Resources Saver
            const tab = p.locator('li[aria-label*="Resources Saver"], [aria-label*="Resources Saver"]')
              .first();
            if (await tab.count().catch(() => 0)) {
              await tab.click({ timeout: 3000 }).catch(() => {});
              log('   выбрана вкладка Resources Saver в DevTools');
            }
          }
        }
      }
      if (!panel) await sleep(500);
    }
    if (!devtools) throw new Error('DevTools не открылся');
    if (!panel) throw new Error('панель Resources Saver не найдена в DevTools');

    // 3) жмём «Save All Resources»
    const btn = panel.locator('#up-save');
    await btn.waitFor({ state: 'visible', timeout: 15000 });
    log('нажимаю «Save All Resources»…');
    await panel.evaluate(() => {
      const b = document.getElementById('up-save');
      b.scrollIntoView();
      b.click();
    });

    // 4) ждём ZIP
    const deadline = Date.now() + 180000;
    let zip = null;
    while (Date.now() < deadline) {
      const z = fs.readdirSync(OUT).filter((f) => f.endsWith('.zip') && !f.endsWith('.crdownload'));
      if (z.length) { zip = z[z.length - 1]; break; }
      await sleep(2000);
    }
    if (!zip) {
      const txt = await panel.textContent('body').catch(() => '');
      log(`состояние панели: ${(txt || '').replace(/\s+/g, ' ').slice(0, 200)}`);
    }
    await browser.close().catch(() => {});
    if (!zip) { log('архив не появился'); process.exit(2); }
    log(`готово: ${path.join(OUT, zip)} (${(fs.statSync(path.join(OUT, zip)).size / 1048576).toFixed(1)} МБ)`);
  } finally {
    try { process.kill(-child.pid); } catch { /* уже закрыт */ }
    try { child.kill('SIGTERM'); } catch { /* уже закрыт */ }
  }
}

main().catch((e) => { console.error('ошибка:', e.message); process.exit(1); });
