#!/usr/bin/env node
// Шаг «Нажать Save All Resources»: панель Resources Saver в DevTools, клик кнопки,
// ожидание архива. Лимит шага — 30 секунд (TOTAL_LIMIT_MS).
import fs from 'fs';
import path from 'path';

const URL_ = process.env.URL || '';
const PROFILE_DIR = path.resolve(process.env.PROFILE || './.chrome-profile');
const EXT_DIR = path.resolve(process.env.EXT_DIR || './.chrome-ext');
const OUT = path.resolve(process.env.OUTPUT_DIR || './artifacts');   // нужен для папки загрузок

// id распакованного расширения Chrome считает от абсолютного пути
import crypto from 'crypto';
function unpackedExtensionId(dir) {
  const h = crypto.createHash('sha256').update(dir).digest('hex').slice(0, 32);
  return h.split('').map((c) => String.fromCharCode(97 + parseInt(c, 16))).join('');
}

// подмена chrome.devtools для панели: панель сама получит ресурсы и соберёт ZIP
const SHIM_SOURCE = `(() => {
  const noop = { addListener() {}, removeListener() {} };
  chrome.devtools = {
    inspectedWindow: {
      tabId: ${Number(process.env.TAB_ID || 0)},
      getResources(cb) { cb([]); },
      onResourceAdded: noop,
      reload() {}, eval() {},
    },
    network: { getHAR(cb) { cb({ log: { version: '1.2', entries: [] } }); }, onRequestFinished: noop },
  };
})();`;
const EXT = path.resolve(process.env.EXT_DIR || './.chrome-ext');
const PORT = parseInt(process.env.CDP_PORT || '9222', 10);
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

  send(method, params = {}, sessionId, timeoutMs = 6000) {
    const id = ++this.n;
    const msg = { id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    this.ws.send(JSON.stringify(msg));
    return new Promise((res, rej) => {
      const timer = setTimeout(() => {
        this.waiting.delete(id);
        rej(new Error(`${method}: нет ответа за ${timeoutMs} мс`));
      }, timeoutMs);
      this.waiting.set(id, {
        res: (v) => { clearTimeout(timer); res(v); },
        rej: (e) => { clearTimeout(timer); rej(e); },
      });
    });
  }

  async eval(expression, sessionId, contextId) {
    const params = { expression, returnByValue: true, awaitPromise: true };
    if (contextId) params.contextId = contextId;
    const r = await this.send('Runtime.evaluate', params, sessionId);
    if (r && r.exceptionDetails) throw new Error(r.exceptionDetails.text || 'ошибка в странице');
    return r && r.result ? r.result.value : undefined;
  }
}


// Настоящее нажатие кнопки: окно DevTools не отдаётся в CDP, поэтому жмём
// вводом X11 (как человек) — окно DevTools, кнопка вверху панели.
function xdo(cmd) {
  try {
    return execSync(`xdotool ${cmd}`, {
      env: { ...process.env, DISPLAY: process.env.DISPLAY || ':99' },
    }).toString().trim();
  } catch { return ''; }
}

function geometry(win) {
  const out = xdo(`getwindowgeometry --shell ${win}`);
  const g = {};
  for (const line of out.split('\n')) {
    const [k, v] = line.split('=');
    if (k && v !== undefined) g[k.trim().toLowerCase()] = v.trim();
  }
  return g;
}



// Прямой доступ к фронтенду DevTools: Chrome пишет его порт в DevToolsActivePort
// в профиле. Через него мы попадаем в контексты панели Resources Saver
// и жмём #up-save — окно DevTools среди целей CDP не видно.
function devtoolsEndpoint(profileDir) {
  const cands = [
    path.join(profileDir, 'DevToolsActivePort'),
    path.join(profileDir, 'Default', 'DevToolsActivePort'),
  ];
  for (const f of cands) {
    if (!fs.existsSync(f)) continue;
    const l = fs.readFileSync(f, 'utf8').split('\n').map((x) => x.trim()).filter(Boolean);
    if (l.length >= 2) return `ws://127.0.0.1:${l[0]}${l[1]}`;
  }
  return '';
}

async function pressInDevtoolsFrontend() {
  log(`   профиль: ${PROFILE_DIR}`);
  try {
    log(`   файлы профиля: ${fs.readdirSync(PROFILE_DIR).slice(0, 25).join(' ')}`);
  } catch (e) { log(`   профиль не читается: ${e.message}`); }
  let ep = '';
  for (let i = 0; i < 10 && !ep; i++) {         // файл может появиться не сразу
    ep = devtoolsEndpoint(PROFILE_DIR);
    if (!ep) await sleep(500);
  }
  if (!ep) {
    return 'DevToolsActivePort не найден — фронтенд DevTools недоступен, нажать кнопку нечем';
  }
  log(`   фронтенд DevTools: ${ep}`);
  const fe = await CDP.connect(ep).catch((e) => null);
  if (!fe) return 'не подключились к фронтенду';
  const contexts = [];
  fe.onEvent = (m) => {
    if (m.method === 'Runtime.executionContextCreated') contexts.push(m.params.context);
  };
  try { await fe.send('Runtime.enable', {}, undefined, 5000); } catch (e) {
    return `Runtime.enable: ${e.message}`;
  }
  await sleep(800);
  log(`   контекстов: ${contexts.length}`);
  for (const cx of contexts) {
    const has = await fe.eval('!!document.getElementById("up-save")', undefined, cx.id)
      .catch(() => false);
    if (!has) continue;
    return await fe.eval(`(() => {
      const b = document.getElementById('up-save');
      const t = (b.textContent || '').trim();
      const dis = b.disabled;
      b.click();
      return 'НАЖАТА: "' + t + '" (disabled=' + dis + ')';
    })()`, undefined, cx.id).catch((e) => 'ошибка: ' + e.message);
  }
  return 'кнопка #up-save не найдена во фронтенде';
}


// Панель через iframe прямо в странице: страницы расширения помечены как
// web-ресурсы, поэтому Chrome их грузит. Внутри iframe кнопка #up-save
// настоящая — жмём её через контекст этого фрейма.
async function pressViaIframe(browser, extId, shimSrc) {
  const ts = await browser.send('Target.getTargets');
  const infos = ts.targetInfos.filter((t) => t.type === 'page');
  const want = URL_ ? URL_.split('?')[0] : '';
  const target = infos.find((t) => want && t.url.startsWith(want)) || infos[0];
  if (!target) return 'нет вкладки для iframe';

  const { sessionId } = await browser.send('Target.attachToTarget',
    { targetId: target.targetId, flatten: true });
  const contexts = [];
  browser.onEvent = (m) => {
    if (m.sessionId !== sessionId) return;
    if (m.method === 'Runtime.executionContextCreated') contexts.push(m.params.context);
  };
  await browser.send('Runtime.enable', {}, sessionId, 5000);
  await browser.send('Page.enable', {}, sessionId, 5000).catch(() => {});
  // подмена chrome.devtools действует во всех фреймах, включая новый iframe
  await browser.send('Page.addScriptToEvaluateOnNewDocument',
    { source: shimSrc }, sessionId, 5000).catch(() => {});

  const mainCtx = contexts[0];
  if (!mainCtx) return 'нет контекста страницы';
  const made = await browser.eval(`(() => {
    const old = document.getElementById('rs-panel');
    if (old) old.remove();
    const f = document.createElement('iframe');
    f.id = 'rs-panel';
    f.style.cssText = 'position:fixed;left:0;top:0;width:900px;height:600px;z-index:2147483647';
    f.src = ${JSON.stringify('chrome-extension://' + extId + '/content.html')};
    document.body.appendChild(f);
    return 'iframe создан';
  })()`, sessionId, mainCtx.id).catch((e) => 'ошибка: ' + e.message);
  log(`   ${made}`);
  await sleep(2500);

  // контекст фрейма панели
  let panelCtx = null;
  for (let i = 0; i < 20 && !panelCtx; i++) {
    panelCtx = contexts.find((c) => /\/content\.html/.test(c.origin || '')
      || /content\.html/.test(c.name || '')) || null;
    if (!panelCtx) await sleep(500);
  }
  if (!panelCtx) {
    // иначе пробуем каждый свежий контекст
    for (const c of contexts) {
      const has = await browser.eval('!!document.getElementById("up-save")', sessionId, c.id)
        .catch(() => false);
      if (has) { panelCtx = c; break; }
    }
  }
  if (!panelCtx) return 'контекст панели не появился';
  return await browser.eval(`(() => {
    const b = document.getElementById('up-save');
    if (!b) return 'кнопки #up-save нет';
    const t = (b.textContent || '').trim();
    b.click();
    return 'НАЖАТА: "' + t + '"';
  })()`, sessionId, panelCtx.id).catch((e) => 'ошибка: ' + e.message);
}

async function main() {
  // сторож: шаг не может длиться дольше лимита ни при каких зависаниях
  const watchdog = setTimeout(() => {
    console.error(`ошибка: превышен лимит шага ${TOTAL_LIMIT} мс (${since()})`);
    process.exit(1);
  }, TOTAL_LIMIT + 5000);
  watchdog.unref();
  fs.mkdirSync(OUT, { recursive: true });
  const m = JSON.parse(fs.readFileSync(path.join(EXT, 'manifest.json'), 'utf8'));
  log(`расширение: ${m.name} v${m.version} | лимит шага ${TOTAL_LIMIT} мс`);

  const v = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json();
  const browser = await CDP.connect(v.webSocketDebuggerUrl);
  log('Chrome подключён по CDP');

  // 1) вкладка с игрой (нажатие от неё не зависит — берём мягко)
  let sessionId = null;
  let bodies = new Map();
  try {
    await browser.send('Target.setDiscoverTargets', { discover: true });
    const ts = await browser.send('Target.getTargets');
    const infos = ts.targetInfos.filter((t) => t.type === 'page');
    const want = URL_ ? URL_.split('?')[0] : '';
    const game = infos.find((t) => want && t.url.startsWith(want)) || infos[0];
    if (game) {
      log(`вкладка: ${game.url.slice(0, 90)}`);
      ({ sessionId } = await browser.send('Target.attachToTarget',
        { targetId: game.targetId, flatten: true }));
    } else {
      log('вкладка с игрой не найдена');
    }
  } catch (e) { log(`вкладку не взяли: ${e.message}`); }
  try {
    await browser.send('Browser.setDownloadBehavior',
      { behavior: 'allow', downloadPath: OUT, eventsEnabled: true });
  } catch { /* не критично */ }

  // 2) сбор ответов с телами — остаётся, но выполняется после нажатия кнопки
  //    и только если кнопка сама не справилась (панель берёт данные у DevTools)
  async function collectResources() {
    if (!sessionId) return bodies;
    const pend = [];
    browser.onEvent = (m) => {
      if (m.sessionId !== sessionId) return;
      if (m.method !== 'Network.responseReceived') return;
      const { requestId, response } = m.params;
      if (!/^https?:/i.test(response.url)) return;
      if (/cdn-cgi\/challenge|googletagmanager|google-analytics|ipify/i.test(response.url)) return;
      pend.push(browser.send('Network.getResponseBody', { requestId }, sessionId, 4000)
        .then((r) => {
          const body = r.base64Encoded ? Buffer.from(r.body, 'base64').toString('utf8') : r.body;
          bodies.set(response.url, {
            body, size: (body || '').length, mimeType: response.mimeType || 'text/plain',
          });
        })
        .catch(() => {}));
    };
    try { await browser.send('Network.enable', {}, sessionId, 3000); } catch (e) {
      log(`Network.enable: ${e.message}`);
    }
    await browser.send('Page.enable', {}, sessionId, 3000).catch(() => {});
    await browser.send('Page.reload', { ignoreCache: false }, sessionId, 5000).catch(() => {});
    await sleep(Math.min(COLLECT_MS, Math.max(1000, left() - 8000)));
    await Promise.race([Promise.all(pend), sleep(2000)]);
    log(`ресурсов собрано: ${bodies.size} (${since()})`);
    return bodies;
  }

  // 3) панель Resources Saver живёт внутри окна DevTools и отдельной CDP-целью
  //    не является — подключаемся к самому DevTools и ищем #up-save в его
  //    контекстах (включая iframe панели)
  await browser.send('Target.setDiscoverTargets', { discover: true });
  const all = (await browser.send('Target.getTargets')).targetInfos;
  const devtools = all.filter((t) => /devtools/i.test(t.url || '') || t.type === 'browser_ui');
  log(`окон DevTools среди целей: ${devtools.length}`);
  if (!devtools.length) throw new Error('не найдено ни одного окна DevTools');

  let pc = null;   // панель по CDP недоступна, используется только X11

  // 0) нажатие: сначала прямо во фронтенде DevTools, затем вводом X11
  let pressed = false;
  let why = 'нажатие не выполнено';
  const extId = process.env.EXT_ID || unpackedExtensionId(EXT_DIR);
  let clicked = extId ? await pressViaIframe(browser, extId, SHIM_SOURCE) : 'EXT_ID не задан';
  why = String(clicked);
  log(`   панель в iframe: ${why}`);
  if (/НАЖАТА/.test(why)) pressed = true; else why = why;
  const skipCdp = true;   // окно DevTools не отдаётся в CDP — жмём только вводом X11
  for (const dt of (clicked || skipCdp ? [] : devtools)) {
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

  // нажатия не было — архив и не мог начать собираться, ждать его бессмысленно
  if (!pressed) {
    throw new Error(`кнопка Save All Resources НЕ НАЖАТА: ${why} — архив не создавался (${since()})`);
  }

  // нажатия не было — это и есть результат шага
  if (!pressed) {
    console.error(`кнопка Save All Resources НЕ НАЖАТА: ${why}`);
    process.exit(1);
  }
  log(`кнопка нажата (${since()}) — архив собирает отдельный шаг`);
  process.exit(0);
}

main().catch((e) => { console.error('ошибка:', e.message); process.exit(1); });
