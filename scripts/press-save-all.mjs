#!/usr/bin/env node
// Нажатие «Save All Resources» в панели Resources Saver настоящего DevTools.
//
// Работаем только через CDP (Playwright к уже запущенному Chrome не подключается):
//   1) собираем все ресурсы страницы (Network.getResponseBody)
//   2) находим панель Resources Saver среди целей (её открыли в DevTools)
//   3) нажимаем в ней #up-save — кнопка «Save All Resources»
//   4) ждём ZIP
import fs from 'fs';
import path from 'path';

const URL_ = process.env.URL || '';
const OUT = path.resolve(process.env.OUTPUT_DIR || './artifacts');
const EXT = path.resolve(process.env.EXT_DIR || './.chrome-ext');
const PORT = parseInt(process.env.CDP_PORT || '9222', 10);
const COLLECT_MS = parseInt(process.env.COLLECT_MS || '20000', 10);
const ZIP_TIMEOUT = parseInt(process.env.ZIP_TIMEOUT_MS || '240000', 10);
const PANEL_TIMEOUT = parseInt(process.env.PANEL_TIMEOUT_MS || '60000', 10);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(...a);

class CDP {
  constructor(ws) { this.ws = ws; this.n = 0; this.waiting = new Map(); this.onEvent = null; }

  static async connect(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => {
      ws.addEventListener('open', res, { once: true });
      ws.addEventListener('error', () => rej(new Error('WebSocket не подключился')), { once: true });
    });
    const c = new CDP(ws);
    ws.addEventListener('message', (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.method) { if (c.onEvent) c.onEvent(m); return; }
      const w = c.waiting.get(m.id);
      if (!w) return;
      c.waiting.delete(m.id);
      m.error ? w.rej(new Error(m.error.message)) : w.res(m.result);
    });
    return c;
  }

  send(method, params = {}, sessionId) {
    const id = ++this.n;
    const msg = { id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    this.ws.send(JSON.stringify(msg));
    return new Promise((res, rej) => this.waiting.set(id, { res, rej }));
  }

  async eval(expression, sessionId) {
    const r = await this.send('Runtime.evaluate',
      { expression, returnByValue: true, awaitPromise: true }, sessionId);
    if (r && r.exceptionDetails) throw new Error(r.exceptionDetails.text || 'ошибка в странице');
    return r && r.result ? r.result.value : undefined;
  }
}

async function listTargets(port) {
  const r = await fetch(`http://127.0.0.1:${port}/json/list`);
  return r.json();
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const m = JSON.parse(fs.readFileSync(path.join(EXT, 'manifest.json'), 'utf8'));
  log(`расширение: ${m.name} v${m.version}`);

  const v = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json();
  const browser = await CDP.connect(v.webSocketDebuggerUrl);
  log('Chrome подключён по CDP');

  // 1) вкладка с игрой
  await browser.send('Target.setDiscoverTargets', { discover: true });
  const ts = await browser.send('Target.getTargets');
  const infos = ts.targetInfos.filter((t) => t.type === 'page');
  const want = URL_ ? URL_.split('?')[0] : '';
  const game = infos.find((t) => want && t.url.startsWith(want)) || infos[0];
  if (!game) throw new Error('не нашёл вкладку с игрой');
  log(`вкладка: ${game.url.slice(0, 100)}`);

  const { sessionId } = await browser.send('Target.attachToTarget',
    { targetId: game.targetId, flatten: true });
  await browser.send('Network.enable', {}, sessionId);
  await browser.send('Page.enable', {}, sessionId);
  await browser.send('Browser.setDownloadBehavior',
    { behavior: 'allow', downloadPath: OUT, eventsEnabled: true });

  // 2) сбор всех ответов с телами
  const bodies = new Map();
  const pending = [];
  browser.onEvent = (m) => {
    if (m.sessionId !== sessionId) return;
    if (m.method === 'Network.responseReceived') {
      const { requestId, response } = m.params;
      if (!/^https?:/i.test(response.url)) return;
      if (/cdn-cgi\/challenge|googletagmanager|google-analytics|ipify/i.test(response.url)) return;
      pending.push(browser.send('Network.getResponseBody', { requestId }, sessionId)
        .then((r) => {
          const body = r.base64Encoded ? Buffer.from(r.body, 'base64').toString('utf8') : r.body;
          bodies.set(response.url, {
            body, size: (body || '').length, mimeType: response.mimeType || 'text/plain',
          });
        })
        .catch(() => {}));
    }
  };
  log('перезагружаю страницу для полного сбора…');
  await browser.send('Page.reload', { ignoreCache: false }, sessionId).catch(() => {});
  await sleep(COLLECT_MS);
  await Promise.race([Promise.all(pending), sleep(5000)]);
  log(`ресурсов собрано: ${bodies.size}`);

  // 3) панель Resources Saver в DevTools
  let panel = null;
  const until = Date.now() + PANEL_TIMEOUT;
  while (Date.now() < until && !panel) {
    const list = await listTargets(PORT).catch(() => []);
    panel = list.find((t) => (t.url || '').includes('/content.html'));
    if (!panel) await sleep(1000);
  }
  if (!panel) throw new Error('панель Resources Saver не найдена среди целей');
  log(`панель: ${panel.url}`);

  // 4) нажимаем «Save All Resources»
  const pc = await CDP.connect(panel.webSocketDebuggerUrl);
  await sleep(1000);
  const clicked = await pc.eval(`(() => {
    const b = document.getElementById('up-save');
    if (!b) return 'кнопки #up-save нет';
    const t = (b.textContent || '').trim();
    b.click();
    return 'нажата: ' + t;
  })()`).catch((e) => 'ошибка: ' + e.message);
  log(`кнопка: ${clicked}`);

  // 5) ждём ZIP
  const dl = Date.now() + ZIP_TIMEOUT;
  let zip = null;
  while (Date.now() < dl) {
    const z = fs.readdirSync(OUT).filter((f) => f.endsWith('.zip') && !f.endsWith('.crdownload'));
    if (z.length) { zip = z[z.length - 1]; break; }
    await sleep(2000);
  }
  if (!zip) {
    const state = await pc.eval('document.body.innerText').catch(() => '');
    log(`состояние панели: ${String(state || '').replace(/\s+/g, ' ').slice(0, 300)}`);
    throw new Error('ZIP не появился после нажатия Save All Resources');
  }
  log(`готово: ${path.join(OUT, zip)} (${(fs.statSync(path.join(OUT, zip)).size / 1048576).toFixed(1)} МБ)`);
  process.exit(0);
}

main().catch((e) => { console.error('ошибка:', e.message); process.exit(1); });
