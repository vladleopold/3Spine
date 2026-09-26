#!/usr/bin/env node
// Шаг «Нажать Save All Resources»: панель Resources Saver в DevTools, клик кнопки,
// ожидание архива. Лимит шага — 30 секунд (TOTAL_LIMIT_MS).
import fs from 'fs';
import path from 'path';

const URL_ = process.env.URL || '';
const OUT = path.resolve(process.env.OUTPUT_DIR || './artifacts');
const EXT = path.resolve(process.env.EXT_DIR || './.chrome-ext');
const PORT = parseInt(process.env.CDP_PORT || '9222', 10);
const COLLECT_MS = parseInt(process.env.COLLECT_MS || '20000', 10);
const PANEL_TIMEOUT = parseInt(process.env.PANEL_TIMEOUT_MS || '20000', 10);
const ZIP_TIMEOUT = parseInt(process.env.ZIP_TIMEOUT_MS || '30000', 10);
const TOTAL_LIMIT = parseInt(process.env.TOTAL_LIMIT_MS || '30000', 10);   // лимит шага

const t0 = Date.now();
const left = () => TOTAL_LIMIT - (Date.now() - t0);
const since = () => `${((Date.now() - t0) / 1000).toFixed(1)}с`;
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

  async eval(expression, sessionId, contextId) {
    const params = { expression, returnByValue: true, awaitPromise: true };
    if (contextId) params.contextId = contextId;
    const r = await this.send('Runtime.evaluate', params, sessionId);
    if (r && r.exceptionDetails) throw new Error(r.exceptionDetails.text || 'ошибка в странице');
    return r && r.result ? r.result.value : undefined;
  }
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const m = JSON.parse(fs.readFileSync(path.join(EXT, 'manifest.json'), 'utf8'));
  log(`расширение: ${m.name} v${m.version} | лимит шага ${TOTAL_LIMIT} мс`);

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
  log(`вкладка: ${game.url.slice(0, 90)}`);

  const { sessionId } = await browser.send('Target.attachToTarget',
    { targetId: game.targetId, flatten: true });
  await browser.send('Network.enable', {}, sessionId);
  await browser.send('Page.enable', {}, sessionId);
  await browser.send('Browser.setDownloadBehavior',
    { behavior: 'allow', downloadPath: OUT, eventsEnabled: true });

  // 2) сбор всех ответов с телами — остаётся, как было
  const bodies = new Map();
  const pending = [];
  browser.onEvent = (m) => {
    if (m.sessionId !== sessionId) return;
    if (m.method !== 'Network.responseReceived') return;
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
  };
  await browser.send('Page.reload', { ignoreCache: false }, sessionId).catch(() => {});
  await sleep(Math.min(COLLECT_MS, Math.max(2000, left() - 12000)));
  await Promise.race([Promise.all(pending), sleep(2000)]);
  log(`ресурсов собрано: ${bodies.size} (${since()})`);

  // 3) панель Resources Saver живёт внутри окна DevTools и отдельной CDP-целью
  //    не является — подключаемся к самому DevTools и ищем #up-save в его
  //    контекстах (включая iframe панели)
  await browser.send('Target.setDiscoverTargets', { discover: true });
  const all = (await browser.send('Target.getTargets')).targetInfos;
  const devtools = all.filter((t) => /devtools/i.test(t.url || '') || t.type === 'browser_ui');
  log(`окон DevTools среди целей: ${devtools.length}`);
  if (!devtools.length) throw new Error('не найдено ни одного окна DevTools');

  let clicked = null;
  for (const dt of devtools) {
    let sid;
    try {
      ({ sessionId: sid } = await browser.send('Target.attachToTarget',
        { targetId: dt.targetId, flatten: true }));
    } catch { continue; }
    const contexts = [];
    browser.onEvent = (m) => {
      if (m.sessionId !== sid) return;
      if (m.method === 'Runtime.executionContextCreated') {
        contexts.push(m.params.context);
      }
    };
    try { await browser.send('Runtime.enable', {}, sid); } catch { continue; }
    await sleep(700);
    log(`   DevTools ${String(dt.url).slice(0, 60)}: контекстов ${contexts.length}`);

    // ищем кнопку в каждом контексте
    for (const cx of contexts) {
      const has = await browser.eval('!!document.getElementById("up-save")', sid, cx.id)
        .catch(() => false);
      if (!has) continue;
      const res = await browser.eval(`(() => {
        const b = document.getElementById('up-save');
        const t = (b.textContent || '').trim();
        const dis = b.disabled;
        b.click();
        return 'НАЖАТА: "' + t + '" (disabled=' + dis + ')';
      })()`, sid, cx.id).catch((e) => 'ошибка: ' + e.message);
      clicked = res;
      log(`   ${res}`);
      break;
    }
    if (clicked) break;
    // панель могла быть не выбрана — выберем её и попробуем ещё раз
    if (!clicked) {
      const opened = await browser.eval(`(() => {
        const el = [...document.querySelectorAll('*')].find((e) => {
          const a = (e.getAttribute && (e.getAttribute('aria-label') || e.getAttribute('title'))) || '';
          return /resources saver/i.test(a) || (e.textContent || '').trim() === 'Resources Saver';
        });
        if (!el) return 'вкладки не найдено';
        el.click();
        return 'вкладка нажата';
      })()`, sid).catch(() => 'ошибка');
      log(`   ${opened}`);
      await sleep(1500);
      contexts.length = 0;
      await browser.send('Runtime.enable', {}, sid).catch(() => {});
      await sleep(700);
      for (const cx of contexts) {
        const has = await browser.eval('!!document.getElementById("up-save")', sid, cx.id)
          .catch(() => false);
        if (!has) continue;
        clicked = await browser.eval(`(() => {
          const b = document.getElementById('up-save');
          const t = (b.textContent || '').trim();
          b.click();
          return 'НАЖАТА: "' + t + '"';
        })()`, sid, cx.id).catch((e) => 'ошибка: ' + e.message);
        log(`   ${clicked}`);
        break;
      }
      if (clicked) break;
    }
  }
  if (!clicked) throw new Error(`кнопка Save All Resources не найдена в DevTools (${since()})`);

  // 4) ждём ZIP в остатке лимита
  const dl = Date.now() + Math.min(ZIP_TIMEOUT, Math.max(2000, left()));
  let zip = null;
  while (Date.now() < dl) {
    const z = fs.readdirSync(OUT).filter((f) => f.endsWith('.zip') && !f.endsWith('.crdownload'));
    if (z.length) { zip = z[z.length - 1]; break; }
    await sleep(1000);
  }
  if (!zip) throw new Error(`ZIP не появился после нажатия кнопки (${since()})`);
  log(`готово: ${path.join(OUT, zip)} (${(fs.statSync(path.join(OUT, zip)).size / 1048576).toFixed(1)} МБ) за ${since()}`);
  process.exit(0);
}

main().catch((e) => { console.error('ошибка:', e.message); process.exit(1); });
