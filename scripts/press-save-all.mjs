#!/usr/bin/env node
// Нажатие «Save All Resources» в панели Resources Saver настоящего DevTools.
// Полный цикл укладывается в 30 секунд: панель уже открыта предыдущим шагом,
// данные берёт сам DevTools, поэтому ничего собирать не нужно.
import fs from 'fs';
import path from 'path';

const URL_ = process.env.URL || '';
const OUT = path.resolve(process.env.OUTPUT_DIR || './artifacts');
const EXT = path.resolve(process.env.EXT_DIR || './.chrome-ext');
const PORT = parseInt(process.env.CDP_PORT || '9222', 10);
const PANEL_TIMEOUT = parseInt(process.env.PANEL_TIMEOUT_MS || '10000', 10);
const ZIP_TIMEOUT = parseInt(process.env.ZIP_TIMEOUT_MS || '15000', 10);
const TOTAL_LIMIT = parseInt(process.env.TOTAL_LIMIT_MS || '30000', 10);

const t0 = Date.now();
const left = () => TOTAL_LIMIT - (Date.now() - t0);
const since = () => `${((Date.now() - t0) / 1000).toFixed(1)}с`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(...a);

class CDP {
  constructor(ws) { this.ws = ws; this.n = 0; this.waiting = new Map(); }

  static async connect(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => {
      ws.addEventListener('open', res, { once: true });
      ws.addEventListener('error', () => rej(new Error('WebSocket не подключился')), { once: true });
    });
    const c = new CDP(ws);
    ws.addEventListener('message', (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.method) return;
      const w = c.waiting.get(m.id);
      if (!w) return;
      c.waiting.delete(m.id);
      m.error ? w.rej(new Error(m.error.message)) : w.res(m.result);
    });
    return c;
  }

  send(method, params = {}) {
    const id = ++this.n;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((res, rej) => this.waiting.set(id, { res, rej }));
  }

  async eval(expression) {
    const r = await this.send('Runtime.evaluate',
      { expression, returnByValue: true, awaitPromise: true });
    if (r && r.exceptionDetails) throw new Error(r.exceptionDetails.text || 'ошибка в странице');
    return r && r.result ? r.result.value : undefined;
  }
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const m = JSON.parse(fs.readFileSync(path.join(EXT, 'manifest.json'), 'utf8'));
  log(`расширение: ${m.name} v${m.version} | лимит цикла ${TOTAL_LIMIT} мс`);

  // 1) панель Resources Saver уже открыта в DevTools — ищем её цель
  let panel = null;
  const until = Date.now() + Math.min(PANEL_TIMEOUT, left());
  while (Date.now() < until && !panel) {
    const list = await fetch(`http://127.0.0.1:${PORT}/json/list`).then((r) => r.json()).catch(() => []);
    panel = list.find((t) => (t.url || '').includes('/content.html'));
    if (!panel) await sleep(500);
  }
  if (!panel) throw new Error(`панель Resources Saver не найдена (${since()})`);
  log(`панель: ${panel.url} (${since()})`);

  // 2) нажимаем «Save All Resources» — данные панель берёт у DevTools сама
  const pc = await CDP.connect(panel.webSocketDebuggerUrl);
  const clicked = await pc.eval(`(() => {
    const b = document.getElementById('up-save');
    if (!b) return 'кнопки #up-save нет';
    const t = (b.textContent || '').trim();
    b.click();
    return 'нажата: ' + t;
  })()`).catch((e) => 'ошибка: ' + e.message);
  log(`кнопка: ${clicked} (${since()})`);

  // 3) ждём ZIP, но не дольше остатка лимита
  const dl = Date.now() + Math.min(ZIP_TIMEOUT, Math.max(2000, left()));
  let zip = null;
  while (Date.now() < dl) {
    const z = fs.readdirSync(OUT).filter((f) => f.endsWith('.zip') && !f.endsWith('.crdownload'));
    if (z.length) { zip = z[z.length - 1]; break; }
    await sleep(1000);
  }
  if (!zip) {
    const state = await pc.eval('document.body.innerText').catch(() => '');
    log(`состояние панели: ${String(state || '').replace(/\s+/g, ' ').slice(0, 200)}`);
    throw new Error(`ZIP не появился за ${since()}`);
  }
  log(`готово: ${path.join(OUT, zip)} (${(fs.statSync(path.join(OUT, zip)).size / 1048576).toFixed(1)} МБ) за ${since()}`);
  process.exit(0);
}

main().catch((e) => { console.error('ошибка:', e.message); process.exit(1); });
