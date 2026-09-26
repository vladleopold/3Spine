#!/usr/bin/env node
// Нажимаем «Save All Resources» в панели Resources Saver.
//
// Playwright не показывает окно DevTools среди своих целей, поэтому работаем
// напрямую с CDP по WebSocket (Node 22, без зависимостей):
//   1) /json/list -> окно DevTools (devtools://)
//   2) в нём кликаем вкладку Resources Saver
//   3) /json/list -> цель панели chrome-extension://<id>/content.html
//   4) в ней кликаем #up-save — это и есть кнопка Save All Resources
import fs from 'fs';
import path from 'path';

const OUT = path.resolve(process.env.OUTPUT_DIR || './artifacts');
const PORT = parseInt(process.env.CDP_PORT || '9222', 10);
const PANEL_TIMEOUT = parseInt(process.env.PANEL_TIMEOUT_MS || '45000', 10);
const ZIP_TIMEOUT = parseInt(process.env.ZIP_TIMEOUT_MS || '180000', 10);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(...a);

async function listTargets() {
  const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
  return r.json();
}

class CDP {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.waiting = new Map();
    this.sessions = new Map();          // sessionId -> { url, type }
    this.listeners = [];                // обработчики событий
  }

  onEvent(fn) { this.listeners.push(fn); }

  static async connect(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => {
      ws.addEventListener('open', res, { once: true });
      ws.addEventListener('error', () => rej(new Error('CDP: не подключился')), { once: true });
    });
    const c = new CDP(ws);
    ws.addEventListener('message', (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      if (m.method) {
        // авто-подключение к фреймам: запоминаем, где панель расширения
        if (m.method === 'Target.attachedToTarget') {
          const si = m.params.sessionId;
          c.sessions.set(si, { url: m.params.targetInfo.url, type: m.params.targetInfo.type });
        }
        for (const fn of c.listeners) fn(m);
        return;
      }
      const w = c.waiting.get(m.id);
      if (!w) return;
      c.waiting.delete(m.id);
      m.error ? w.rej(new Error(m.error.message)) : w.res(m.result);
    });
    return c;
  }

  send(method, params = {}, sessionId) {
    const id = ++this.id;
    const msg = { id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    this.ws.send(JSON.stringify(msg));
    return new Promise((res, rej) => this.waiting.set(id, { res, rej }));
  }

  async eval(expression, sessionId) {
    const r = await this.send('Runtime.evaluate',
      { expression, awaitPromise: true, returnByValue: true }, sessionId);
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text || 'ошибка в странице');
    return r.result ? r.result.value : undefined;
  }
}

const CLICK_TAB = `(() => {
  const all = [...document.querySelectorAll('*')];
  const hit = all.find((e) => {
    const t = ((e.getAttribute && (e.getAttribute('aria-label') || e.getAttribute('title'))) || '').trim();
    return /resources saver/i.test(t);
  }) || all.find((e) => (e.textContent || '').trim() === 'Resources Saver');
  if (!hit) return 'вкладка не найдена';
  hit.click();
  return 'вкладка нажата';
})()`;

const CLICK_SAVE = `(() => {
  const b = document.getElementById('up-save');
  if (!b) return 'кнопки #up-save нет';
  b.scrollIntoView();
  b.click();
  return 'кнопка нажата: ' + (b.textContent || '').trim();
})()`;

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const all = await listTargets();
  log(`целей в Chrome: ${all.length}`);
  for (const t of all.slice(0, 12)) log(`   [${t.type}] ${t.url.slice(0, 100)}`);

  const devtools = all.find((t) => t.url.startsWith('devtools://'));
  if (!devtools) {
    const panel = all.find((t) => t.url.startsWith('chrome-extension://') && t.url.includes('content.html'));
    log(panel ? 'панель есть, окна DevTools нет — жму прямо в панели' : 'нет ни окна DevTools, ни панели');
    if (!panel) throw new Error('DevTools не открылся: нет ни devtools://, ни content.html');
    const c = await CDP.connect(panel.webSocketDebuggerUrl);
    log(await c.eval(CLICK_SAVE));
  } else {
    log(`окно DevTools: ${devtools.url.slice(0, 90)}`);
    const dt = await CDP.connect(devtools.webSocketDebuggerUrl);
    // панель расширения — фрейм внутри окна DevTools, подхватываем его авто-attach'ем
    await dt.send('Target.setAutoAttach',
      { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });
    log(await dt.eval(CLICK_TAB));

    const deadline = Date.now() + PANEL_TIMEOUT;
    let panelSession = null;
    while (Date.now() < deadline && !panelSession) {
      for (const [sid, info] of dt.sessions) {
        if (info.url && info.url.startsWith('chrome-extension://') && info.url.includes('content.html')) {
          panelSession = sid;
          log(`панель в DevTools: ${info.url}`);
          break;
        }
      }
      if (!panelSession) {
        // вкладка могла не открыться с первого раза — жмём ещё раз
        await dt.eval(CLICK_TAB).catch(() => {});
        await sleep(1500);
      }
    }
    if (!panelSession) throw new Error('фрейм панели Resources Saver не появился в DevTools');
    log(await dt.eval(CLICK_SAVE, panelSession));
  }

  const zipDeadline = Date.now() + ZIP_TIMEOUT;
  let zip = null;
  while (Date.now() < zipDeadline) {
    const z = fs.readdirSync(OUT).filter((f) => f.endsWith('.zip') && !f.endsWith('.crdownload'));
    if (z.length) { zip = z[z.length - 1]; break; }
    await sleep(2000);
  }
  if (!zip) throw new Error('ZIP не появился после нажатия Save All Resources');
  log(`готово: ${path.join(OUT, zip)} (${(fs.statSync(path.join(OUT, zip)).size / 1048576).toFixed(1)} МБ)`);
}

main().catch((e) => { console.error('ошибка:', e.message); process.exit(1); });
