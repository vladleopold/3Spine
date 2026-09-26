#!/usr/bin/env node
// Шаг «Нажать Save All Resources»: панель Resources Saver в DevTools, клик кнопки,
// ожидание архива. Лимит шага — 30 секунд (TOTAL_LIMIT_MS).
import fs from 'fs';
import { execSync } from 'child_process';
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

/** ID расширения для SHOW_PANEL (env или вычисленный по пути). */
function extIdGuess() {
  return process.env.EXT_ID || unpackedExtensionId(EXT_DIR);
}

// подмена chrome.devtools для панели: панель сама получит ресурсы и соберёт ZIP
const SHIM_SOURCE = `(() => {
  const RESOURCES = __RESOURCES__;
  const HAR = __HAR__;
  const noop = { addListener() {}, removeListener() {} };
  chrome.devtools = {
    inspectedWindow: {
      tabId: ${Number(process.env.TAB_ID || 0)},
      getResources(cb) { cb(RESOURCES.map((r) => ({ url: r.url, content: r.body, size: r.size }))); },
      onResourceAdded: noop,
      reload() {}, eval() {},
    },
    network: { getHAR(cb) { cb({ log: { version: '1.2', entries: HAR } }); }, onRequestFinished: noop },
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
  // отдельная пустая вкладка: игровую не трогаем, она тяжёлая
  const made = await browser.send('Target.createTarget', { url: 'about:blank' });
  const target = (await browser.send('Target.getTargets')).targetInfos
    .find((t) => t.targetId === made.targetId);
  if (!target) return 'не создалась вкладка для панели';

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
  const src = 'chrome-extension://' + extId + '/content.html';
  const injected = await browser.eval(`(() => {
    window.__rs = 'создаём iframe';
    const old = document.getElementById('rs-panel');
    if (old) old.remove();
    const f = document.createElement('iframe');
    f.id = 'rs-panel';
    f.style.cssText = 'position:fixed;left:0;top:0;width:900px;height:600px;z-index:2147483647';
    f.onload = () => { window.__rs = 'iframe загрузился'; };
    f.onerror = () => { window.__rs = 'iframe не загрузился'; };
    f.src = ${JSON.stringify(src)};
    document.body.appendChild(f);
    return 'iframe создан: ' + f.src;
  })()`, sessionId, mainCtx.id).catch((e) => 'ошибка: ' + e.message);
  log(`   ${injected}`);
  await sleep(3000);
  const st = await browser.eval('window.__rs || "нет статуса"', sessionId, mainCtx.id)
    .catch(() => 'нет статуса');
  log(`   статус iframe: ${st}`);
  log(`   контексты: ${contexts.map((c) => c.origin).join(' | ')}`);

  // панель — отдельная цель (OOPIF) с URL content.html: ищем её среди всех целей
  let panelTargetInfo = null;
  for (let i = 0; i < 20 && !panelTargetInfo; i++) {
    await browser.send('Target.setDiscoverTargets', { discover: true });
    const infos = (await browser.send('Target.getTargets')).targetInfos || [];
    panelTargetInfo = infos.find((t) => /\/content\.html/.test(t.url || '')) || null;
    if (!panelTargetInfo) await sleep(500);
  }
  if (panelTargetInfo) {
    log(`   цель панели: [${panelTargetInfo.type}] ${panelTargetInfo.url.slice(0, 80)}`);
    let pw = null, pwSess = null;
    if (panelTargetInfo.webSocketDebuggerUrl) {
      pw = await CDP.connect(panelTargetInfo.webSocketDebuggerUrl).catch(() => null);
    }
    if (!pw) {
      try {
        ({ sessionId: pwSess } = await browser.send('Target.attachToTarget',
          { targetId: panelTargetInfo.targetId, flatten: true }, undefined, 5000));
      } catch (e) { log(`   attach панели: ${e.message}`); }
    }
    const evalP = (expr, ctxId) => (pw
      ? pw.eval(expr, undefined, ctxId, 3000)
      : browser.eval(expr, pwSess, ctxId, 3000));
    const sendP = (m, pr) => (pw ? pw.send(m, pr, undefined, 4000) : browser.send(m, pr, pwSess, 4000));
    const ctxs = [];
    const onC = (m) => {
      if (m.method === 'Runtime.executionContextCreated') ctxs.push(m.params.context);
    };
    if (pw) pw.onEvent = onC; else browser.onEvent = onC;
    await sendP('Runtime.enable', {}).catch((e) => log(`   Runtime.enable: ${e.message}`));
    await sleep(1000);
    log(`   контекстов панели: ${ctxs.length}`);
    for (const c of ctxs) {
      const has = await evalP('!!document.getElementById("up-save")', c.id).catch(() => false);
      if (!has) continue;
      return await evalP(`(() => {
        const b = document.getElementById('up-save');
        const t = (b.textContent || '').trim();
        b.click();
        return 'НАЖАТА: "' + t + '"';
      })()`, c.id).catch((e) => 'ошибка: ' + e.message);
    }
    // контекстов нет — пробуем без контекста
    return await evalP(`(() => {
      const b = document.getElementById('up-save');
      if (!b) return 'кнопки #up-save нет';
      const t = (b.textContent || '').trim();
      b.click();
      return 'НАЖАТА: "' + t + '"';
    })()`).catch((e) => 'ошибка: ' + e.message);
  }
  log('   цель панели среди целей не появилась');

  // фрейм панели берём из дерева фреймов — это запасной путь
  const tree = await browser.send('Page.getFrameTree', {}, sessionId, 3000)
    .catch(() => null);
  let frameId = '';
  if (tree && tree.frameTree) {
    const walk = (n) => {
      if (n.frame && /\/content\.html/.test(n.frame.url || '')) { frameId = n.frame.id; return true; }
      return (n.childFrames || []).some(walk);
    };
    walk(tree.frameTree);
  }
  log(`   фрейм панели: ${frameId || 'не найден'}`);

  // панель может быть без отдельного фрейма и цели — тогда кнопка лежит
  // в одном из контекстов страницы, который мы уже собрали
  for (const c of contexts) {
    const has = await browser.eval('!!document.getElementById("up-save")', sessionId, c.id, 2000)
      .catch(() => false);
    log(`   контекст ${c.id}: кнопка ${has ? 'есть' : 'нет'}`);
    if (!has) continue;
    const res = await browser.eval(`(() => {
      const b = document.getElementById('up-save');
      const t = (b.textContent || '').trim();
      b.click();
      return 'НАЖАТА: "' + t + '"';
    })()`, sessionId, c.id, 3000).catch((e) => 'ошибка: ' + e.message);
    log(`   ${res}`);
    return res;
  }

  if (!frameId) return 'фрейм панели не найден';
  const world = await browser.send('Page.createIsolatedWorld',
    { frameId, worldName: 'rs-world', grantUniveralAccess: true }, sessionId, 4000)
    .catch(() => null);
  const ctxId = world && world.executionContextId;
  if (!ctxId) return 'не создался изолированный мир панели';
  log(`   контекст панели: ${ctxId}`);
  return await browser.eval(`(() => {
    const b = document.getElementById('up-save');
    if (!b) return 'кнопки #up-save нет';
    const t = (b.textContent || '').trim();
    b.click();
    return 'НАЖАТА: "' + t + '"';
  })()`, sessionId, ctxId, 4000).catch((e) => 'ошибка: ' + e.message);
}


async function collectFromGame(browser) {
  const ts = await browser.send('Target.getTargets');
  const infos = ts.targetInfos.filter((t) => t.type === 'page');
  const want = URL_ ? URL_.split('?')[0] : '';
  const game = infos.find((t) => want && t.url.startsWith(want)) || infos[0];
  if (!game) return { sessionId: null, bodies: new Map() };
  log(`   игра: ${game.url.slice(0, 80)}`);
  let sessionId = null;
  try {
    ({ sessionId } = await browser.send('Target.attachToTarget',
      { targetId: game.targetId, flatten: true }, undefined, 8000));
  } catch (e) { log(`   attach не удался: ${e.message}`); }
  if (!sessionId) return { sessionId: null, bodies: new Map() };
  const bodies = new Map();
  const pend = [];
  browser.onEvent = (m) => {
    if (m.sessionId !== sessionId || m.method !== 'Network.responseReceived') return;
    const { requestId, response } = m.params;
    if (!/^https?:/i.test(response.url)) return;
    if (/cdn-cgi\/challenge|googletagmanager|google-analytics|ipify/i.test(response.url)) return;
    pend.push(browser.send('Network.getResponseBody', { requestId }, sessionId, 4000)
      .then((r) => {
        const body = r.base64Encoded ? Buffer.from(r.body, 'base64').toString('utf8') : r.body;
        bodies.set(response.url, { body, size: (body || '').length,
          mimeType: response.mimeType || 'text/plain' });
      }).catch(() => {}));
  };
  // игра на WebGL не отвечает на CDP быстро — потолок 4 с, иначе съедаем лимит шага
  try { await browser.send('Network.enable', {}, sessionId, 1500); } catch (e) { log(`   Network.enable: ${e.message}`); }
  try { await browser.send('Page.enable', {}, sessionId, 1000); } catch { /* не критично */ }
  await sleep(2500);
  await Promise.race([Promise.all(pend), sleep(500)]);
  log(`   ресурсов собрано: ${bodies.size} (${since()})`);
  return { sessionId, bodies };
}


// Открываем панель Resources Saver программно через API фронтенда DevTools.
// X11-ввод (палитра) не срабатывает, а у фронтенда есть свой доступ.
const LIST_PANELS = `(() => {
  try {
    const iv = (globalThis.UI && UI.inspectorView) || null;
    if (!iv) return 'нет UI.inspectorView';
    const keys = Object.keys(iv).filter((k) => /panel|tab/i.test(k));
    let extra = '';
    for (const k of ['panels', 'tabbedPane', '_tabbedPane', 'view']) {
      const v = iv[k];
      if (v && Array.isArray(v.tabs)) {
        extra += ' ' + k + '=[' + v.tabs.map((t) => t.id || t.name).join(',') + ']';
      }
    }
    return 'ключи: ' + keys.join(',') + ' |' + extra;
  } catch (e) { return 'ошибка: ' + e.message; }
})()`;

const SHOW_PANEL = `(async () => {
  try {
    const iv = (globalThis.UI && UI.inspectorView) || null;
    if (!iv) return 'нет UI.inspectorView';
    const ids = ${JSON.stringify([])};
    // 1) id панели = id расширения
    for (const want of ids.concat(['__EXT_ID__'])) {
      if (!want) continue;
      try { await iv.showPanel(want); return 'панель открыта по id: ' + want; }
      catch (e) { /* пробуем следующий */ }
    }
    // 2) любой доступный список панелей
    for (const k of ['_tabbedPane', 'tabbedPane', 'panels']) {
      const v = iv[k];
      if (v && Array.isArray(v.tabs)) {
        for (const t of v.tabs) {
          const id = String(t.id || t.name || '');
          if (!/resource/i.test(id)) continue;
          await iv.showPanel(id);
          return 'панель открыта: ' + id;
        }
      }
    }
    return 'панель не найдена';
  } catch (e) { return 'ошибка: ' + e.message; }
})()`;

// Настоящая панель Resources Saver открытой вкладкой: та же content.html
// с настоящей кнопкой #up-save. Шэм ставится ДО загрузки страницы, иначе
// content.js не увидит chrome.devtools и не соберёт ресурсы.
async function pressViaPanelTab(browser, extId, shimSrc) {
  const url = 'chrome-extension://' + extId + '/content.html';
  let sessionId = null; let targetId = null;
  try {
    ({ targetId } = await browser.send('Target.createTarget', { url: 'about:blank' }, undefined, 5000));
  } catch (e) { return 'вкладку панели не создали: ' + e.message; }
  try {
    ({ sessionId } = await browser.send('Target.attachToTarget', { targetId, flatten: true }, undefined, 5000));
  } catch (e) { return 'к вкладке панели не подключились: ' + e.message; }

  const contexts = [];
  browser.onEvent = (m) => {
    if (m.sessionId !== sessionId) return;
    if (m.method === 'Runtime.executionContextCreated') contexts.push(m.params.context);
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails || {};
      log(`   панель бросила: ${d.text || ''} ${(d.exception || {}).description || ''}`.slice(0, 170));
    }
  };
  try { await browser.send('Runtime.enable', {}, sessionId, 3000); } catch (e) { return 'Runtime.enable: ' + e.message; }
  try { await browser.send('Page.enable', {}, sessionId, 3000); } catch {}

  const wrapped = '(() => { try { if (!globalThis.chrome) globalThis.chrome = {}; ' + shimSrc
    + ' } catch (e) { globalThis.__rsShimError = String(e); } })();';
  await browser.send('Page.addScriptToEvaluateOnNewDocument', { source: wrapped }, sessionId, 3000)
    .catch((e) => log(`   шэм не поставился: ${e.message}`));
  await browser.send('Page.navigate', { url }, sessionId, 6000).catch((e) => log(`   переход: ${e.message}`));

  const mainCtx = async () => {
    for (let i = 0; i < 34; i++) {
      const def = contexts.filter((c) => c.auxData && c.auxData.isDefault);
      if (def.length) return def[def.length - 1];
      await sleep(300);
    }
    return contexts[0] || null;
  };
  let main = await mainCtx();
  if (!main) { log('   контекстов панели: 0'); return 'контекст панели не появился'; }
  log(`   контекстов панели: ${contexts.length}`);

  // если биндинги Chrome перекрыли шэм — ставим шэм ещё раз и перезагружаем панель
  const hasDevtools = await browser.eval('!!(globalThis.chrome && chrome.devtools)', sessionId, main.id, 3000)
    .catch(() => false);
  if (!hasDevtools) {
    log('   chrome.devtools в панели нет — ставим шэм заново и перезагружаем');
    await browser.eval(shimSrc, sessionId, main.id, 3000).catch((e) => log(`   шэм в странице: ${e.message}`));
    contexts.length = 0;
    await browser.send('Page.reload', { ignoreCache: true }, sessionId, 6000).catch(() => {});
    main = await mainCtx();
    if (!main) return 'после шэма контекст панели не появился';
  }

  let has = false;
  for (let i = 0; i < 30 && !has; i++) {
    has = !!(await browser.eval('!!document.getElementById("up-save")', sessionId, main.id, 2500)
      .catch(() => false));
    if (!has) await sleep(300);
  }
  if (!has) {
    const diag = await browser.eval(`(() => {
      const b = document.body;
      return 'url=' + String(location.href).slice(0, 70)
        + ' || текст=' + (b ? (b.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 220) : 'нет body')
        + ' || shim=' + (globalThis.__rsShimError || 'ок')
        + ' || devtools=' + (globalThis.chrome && chrome.devtools ? 'есть' : 'нет')
        + ' || входов=' + (b ? b.querySelectorAll('*').length : 0);
    })()`, sessionId, main.id, 3000).catch((e) => 'диагностика: ' + e.message);
    log(`   ${diag}`);
    // проверяем, доступна ли страница панели вообще: WAR работает — значит
    //Extension.getURL доступна, а блокирует именно навигация вкладки
    const probe = await browser.eval(`fetch('chrome-extension://' + ${JSON.stringify(extId)} + '/content.html')
      .then((r) => 'fetch панели: ' + r.status + ' байт=' + r.headers.get('content-length'))
      .catch((e) => 'fetch панели: ' + e.message)`, sessionId, main.id, 4000)
      .catch((e) => 'fetch панели: ' + e.message);
    log(`   ${probe}`);
    return 'в панели кнопки #up-save нет';
  }

  return await browser.eval(`(() => {
    const b = document.getElementById('up-save');
    const t = (b.textContent || '').trim();
    b.click();
    return 'НАЖАТА: "' + t + '"';
  })()`, sessionId, main.id, 4000).catch((e) => 'ошибка: ' + e.message);
}


// Окон DevTools в целях несколько, и почти все — пустые шеллы без
// фронтенда. Перебираем их и работаем в том, где приложение реально живёт.
async function pressInAnyDevtoolsWindow(browser, devtools) {
  for (const t of devtools) {
    let sid = null;
    try {
      ({ sessionId: sid } = await browser.send('Target.attachToTarget',
        { targetId: t.targetId, flatten: true }, undefined, 4000));
    } catch { continue; }
    const ctxs = [];
    browser.onEvent = (m) => {
      if (m.sessionId !== sid) return;
      if (m.method === 'Runtime.executionContextCreated') ctxs.push(m.params.context);
    };
    try { await browser.send('Runtime.enable', {}, sid, 4000); } catch { continue; }
    await sleep(1200);
    let best = null; let bestN = 0;
    for (const cx of ctxs) {
      const n = await browser.eval('document.querySelectorAll("*").length', sid, cx.id, 2500)
        .catch(() => 0);
      const ttl = await browser.eval('String(document.title || "")', sid, cx.id, 2500)
        .catch(() => '');
      log(`   окно ${t.targetId.slice(0, 6)} контекст ${cx.id}: элементов=${n} title=${String(ttl).slice(0, 24)}`);
      if (n < 200) {
        const d = await browser.eval(`(() => {
          const ks = Object.keys(globalThis).filter((k) => /^(UI|Root|Inspector|Common|DevTools|Host|Protocol)/.test(k));
          return 'url=' + String(location.href).slice(0, 90)
            + ' || глобалы=[' + ks.join(',') + ']'
            + ' || html=' + String(document.documentElement.outerHTML).replace(/\\s+/g, ' ').slice(0, 300);
        })()`, sid, cx.id, 3000).catch((e) => 'диагностика: ' + e.message);
        log(`      ${d}`);
      }
      if (n > bestN) { bestN = n; best = cx; }
    }
    if (!best) continue;
    // весь UI DevTools лежит в shadow root, а приватные API (UI.InspectorView)
    // в сборке 153 недоступны — работаем через настоящий UI: вкладка и кнопка
    const isDevtoolsDoc = await browser.eval(
      "String(location.href).startsWith('devtools://')", sid, best.id, 3000).catch(() => false);
    if (!isDevtoolsDoc) continue;

    const SHADOW_ROOTS = `root => {
      const out = [root];
      const scan = (r) => {
        const all = r.querySelectorAll ? r.querySelectorAll('*') : [];
        for (const el of all) if (el.shadowRoot) { out.push(el.shadowRoot); scan(el.shadowRoot); }
      };
      scan(root);
      return out;
    }`;

    // официальный хук фронтенда DevTools: открыть панель по имени
    {
      const api = await browser.eval(`(async () => {
        try {
          if (!globalThis.DevToolsAPI) return 'DevToolsAPI нет';
          const keys = Object.keys(DevToolsAPI).slice(0, 12).join(',');
          let res = '';
          try { await DevToolsAPI.showPanel('Resources Saver'); res = 'showPanel Resources Saver: ок'; }
          catch (e) { res = 'showPanel: ' + e.message; }
          return res + ' | методы: ' + keys;
        } catch (e) { return 'DevToolsAPI: ' + e.message; }
      })()`, sid, best.id, 6000).catch((e) => 'DevToolsAPI: ошибка ' + e.message);
      log(`   ${api}`);
      await sleep(1500);
    }

    // Панель расширения — отдельный документ: ищем её среди целей, фреймов
    // и текста в тенях. В панели работает настоящий chrome.devtools.
    {
      const CLICK = `(() => {
        const b = document.getElementById('up-save');
        if (!b) return 'кнопки #up-save нет';
        const t = (b.textContent || '').trim();
        b.click();
        return 'НАЖАТА: "' + t + '"';
      })()`;

      const tree = await browser.send('Page.getFrameTree', {}, sid, 5000).catch(() => null);
      const frames = [];
      if (tree && tree.frameTree) {
        const walk = (n, d) => {
          frames.push({ id: n.frame.id, url: n.frame.url || '', d });
          (n.childFrames || []).forEach((c) => walk(c, d + 1));
        };
        walk(tree.frameTree, 0);
      }
      log(`   фреймов после открытия панели: ${frames.length}`);
      for (const f of frames) log(`      ${'  '.repeat(f.d)}[${String(f.url).slice(0, 88)}]`);
      for (const f of frames.slice(1)) {
        const w = await browser.send('Page.createIsolatedWorld',
          { frameId: f.id, worldName: 'panel', grantUniveralAccess: true }, sid, 5000).catch(() => null);
        if (!w || !w.executionContextId) { log(`   фрейм ${f.id.slice(0, 6)}: мир не создался`); continue; }
        const res = await browser.eval(CLICK, sid, w.executionContextId, 3500).catch((e) => 'ошибка: ' + e.message);
        log(`   фрейм ${f.id.slice(0, 6)}: ${res}`);
        if (/НАЖАТА/.test(String(res))) return res;
      }

      // текст кнопки должен появиться в тенях окна DevTools, если панель открыта
      const inShadows = await browser.eval(`(() => {
        const roots = (${SHADOW_ROOTS})(document);
        for (const r of roots) for (const el of r.querySelectorAll('*')) {
          const t = (el.innerText || '').trim();
          if (!/save all/i.test(t) || t.length > 60) continue;
          return 'нашли: <' + el.tagName + ' id=' + (el.id || '-') + ' class='
            + String(el.className).slice(0, 30) + '> текст="' + t.slice(0, 30) + '"';
        }
        return 'текста Save All в тенях нет (корней: ' + roots.length + ')';
      })()`, sid, best.id, 4000).catch((e) => 'ошибка: ' + e.message);
      log(`   ${inShadows}`);

      // и среди целей: панель может быть отдельной страницей
      const infos = (await browser.send('Target.getTargets', {}, undefined, 5000).catch(() => null) || {}).targetInfos || [];
      log(`   целей сейчас: ${infos.length}`);
      const panelTargets = infos.filter((t) => /content\.html|${extIdGuess()}/.test(String(t.url || '')));
      for (const t of panelTargets) {
        log(`   цель панели: [${t.type}] ${String(t.url).slice(0, 80)}`);
        let psid = null;
        try {
          ({ sessionId: psid } = await browser.send('Target.attachToTarget',
            { targetId: t.targetId, flatten: true }, undefined, 4000));
        } catch { continue; }
        const pctx = [];
        browser.onEvent = (m) => {
          if (m.sessionId !== psid) return;
          if (m.method === 'Runtime.executionContextCreated') pctx.push(m.params.context);
        };
        try { await browser.send('Runtime.enable', {}, psid, 4000); } catch { continue; }
        await sleep(900);
        for (const c of pctx) {
          const res = await browser.eval(CLICK, psid, c.id, 3500).catch((e) => 'ошибка: ' + e.message);
          log(`   контекст ${c.id}: ${res}`);
          if (/НАЖАТА/.test(String(res))) return res;
        }
      }
    }

    for (let i = 0; i < 8; i++) {
      const tabs = await browser.eval(`(() => {
        const roots = (${SHADOW_ROOTS})(document);
        const names = [];
        for (const r of roots) for (const el of r.querySelectorAll('*')) {
          const role = el.getAttribute && el.getAttribute('role');
          if (role !== 'tab') continue;
          const t = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
          if (t && t.length < 40) names.push(t);
        }
        return 'вкладки: ' + [...new Set(names)].join(' | ').slice(0, 300);
      })()`, sid, best.id, 3000).catch((e) => 'вкладки: ошибка ' + e.message);
      log(`   ${tabs}`);

      if (i === 0) {
        const dump = await browser.eval(`(() => {
          const roots = (${SHADOW_ROOTS})(document);
          const seen = [];
          for (const r of roots) for (const el of r.querySelectorAll('*')) {
            const cls = String((el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className) || '');
            if (!/tab/i.test(cls)) continue;
            const t = (el.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 26);
            seen.push(cls.split(' ').slice(0, 2).join('.') + '=' + t);
          }
          const main = [...new Set(seen)].filter((x) => /tabbed-pane-header-tab=/.test(x));
          return 'главные вкладки: ' + main.map((x) => x.split('=')[1]).filter(Boolean).join(' | ').slice(0, 400);
        })()`, sid, best.id, 4000).catch((e) => 'дамп: ошибка ' + e.message);
        log(`   классы вкладок: ${dump}`);
      }

      // панель расширения может быть не в строке вкладок, а в меню More tools
      const CLICK_TAB = `(() => {
        const roots = (${SHADOW_ROOTS})(document);
        let best = null;
        for (const r of roots) for (const el of r.querySelectorAll('*')) {
          const t = (el.innerText || el.textContent || '').trim();
          if (!/resources saver/i.test(t)) continue;
          if (t.length > 40) continue;
          if (!best || el.querySelectorAll('*').length < best.querySelectorAll('*').length) best = el;
        }
        if (!best) return 'вкладка Resources Saver в тенях не найдена';
        (best.querySelector('*') || best).click();
        return 'вкладка Resources Saver нажата: <' + best.tagName + ' class='
          + String(best.className).slice(0, 40) + '> текст="' + (best.innerText || '').trim().slice(0, 24) + '"';
      })()`;
      const OPEN_MORE = `(() => {
        const roots = (${SHADOW_ROOTS})(document);
        for (const r of roots) for (const el of r.querySelectorAll('*')) {
          const cls = String(el.className || '');
          if (!/drop-down/i.test(cls)) continue;
          (el.querySelector('button') || el).click();
          return 'меню More tools открыто: ' + cls.split(' ')[0];
        }
        return 'кнопка More tools не найдена';
      })()`;
      const DUMP_MENU = `(() => {
        const roots = (${SHADOW_ROOTS})(document);
        const out = [];
        for (const r of roots) for (const el of r.querySelectorAll('*')) {
          const cls = String(el.className || '');
          if (!/menu|drop-down|context/i.test(cls)) continue;
          const t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
          if (!t || t.length > 300) continue;
          out.push(cls.split(' ').slice(0, 2).join('.') + '=[' + t.slice(0, 120) + ']');
        }
        return [...new Set(out)].slice(0, 12).join(' || ');
      })()`;

      let tabState = await browser.eval(CLICK_TAB, sid, best.id, 3000)
        .catch((e) => 'вкладка: ошибка ' + e.message);
      log(`   ${tabState}`);
      if (/не найдена/.test(String(tabState))) {
        const dd = await browser.eval(OPEN_MORE, sid, best.id, 3000)
          .catch((e) => 'меню: ошибка ' + e.message);
        log(`   ${dd}`);
        await sleep(800);
        const menu = await browser.eval(DUMP_MENU, sid, best.id, 3000)
          .catch((e) => 'дамп меню: ошибка ' + e.message);
        log(`   ${menu}`);
        tabState = await browser.eval(CLICK_TAB, sid, best.id, 3000)
          .catch((e) => 'вкладка: ошибка ' + e.message);
        log(`   ${tabState}`);
      }

      const r = await browser.eval(`(() => {
        const roots = (${SHADOW_ROOTS})(document);
        for (const r2 of roots) {
          const b = r2.querySelector('#up-save');
          if (!b) continue;
          const t2 = (b.textContent || '').trim();
          b.click();
          return 'НАЖАТА: "' + t2 + '"';
        }
        return 'в тенях кнопки #up-save нет';
      })()`, sid, best.id, 4000).catch((e) => 'ошибка: ' + e.message);
      log(`   ${r}`);
      if (/НАЖАТА/.test(String(r))) return r;
      await sleep(700);
      continue;
    }
    if (false) {
      const tabs = await browser.eval(`(() => {
        try {
          const view = UI.InspectorView.instance();
          const ids = (view.tabbedPane.tabs || []).map((t) => (t.id || '') + '|' + (t.title || ''));
          return 'вкладки: ' + ids.join(' ; ').slice(0, 220);
        } catch (e) { return 'вкладки: ошибка ' + e.message; }
      })()`, sid, best.id, 3000).catch((e) => 'вкладки: ошибка ' + e.message);
      log(`   ${tabs}`);
      const shown = await browser.eval(`(async () => {
        try {
          const view = UI.InspectorView.instance();
          const tabs = view.tabbedPane.tabs || [];
          for (const t of tabs) {
            if (!/resource/i.test(String(t.id || '') + ' ' + String(t.title || ''))) continue;
            await view.showPanel(t.id);
            return 'панель открыта: ' + t.id;
          }
          return 'вкладка Resources Saver не найдена';
        } catch (e) { return 'showPanel: ' + e.message; }
      })()`, sid, best.id, 4000).catch((e) => 'showPanel: ' + e.message);
      log(`   ${shown}`);
      const r = await browser.eval(`(() => {
        const walk = (root) => {
          const b = root.querySelector && root.querySelector('#up-save');
          if (b) return b;
          const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
          for (const el of all) { if (el.shadowRoot) { const r2 = walk(el.shadowRoot); if (r2) return r2; } }
          return null;
        };
        const b = walk(document);
        if (!b) return 'в тенях кнопки нет';
        const t2 = (b.textContent || '').trim();
        b.click();
        return 'НАЖАТА: "' + t2 + '"';
      })()`, sid, best.id, 4000).catch((e) => 'ошибка: ' + e.message);
      log(`   ${r}`);
      if (/НАЖАТА/.test(String(r))) return r;
      await sleep(700);
    }
  }
  return 'ни в одном окне DevTools кнопки #up-save нет';
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

  // Chrome 137+ больше не грузит --load-extension, поэтому расширение
  // ставим через CDP — иначе все chrome-extension:// страницы блокируются
  try {
    const loaded = await browser.send('Extensions.loadUnpacked', { path: EXT }, undefined, 10000);
    log(`   расширение загружено через CDP: ${(loaded && loaded.id) || 'ок'}`);
  } catch (e) { log(`   Extensions.loadUnpacked: ${e.message}`); }

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
  let seenTargets = false;
  let why = 'нажатие не выполнено';
  // окно DevTools как CDP-цель: в нём и живёт панель с кнопкой.
  // /json/list не показывает фронтенд DevTools — берём Target.getTargets
  // у браузерной сессии и НЕ фильтруем по типу.
  const devtoolsTarget = async () => {
    await browser.send('Target.setDiscoverTargets', { discover: true });
    const infos = (await browser.send('Target.getTargets')).targetInfos || [];
    if (!seenTargets) {
      log(`   всего целей: ${infos.length}`);
      for (const t of infos) log(`      [${t.type}] ${String(t.url).slice(0, 90)}`);
      seenTargets = true;
    }
    return infos.find((t) => /devtools/i.test(t.url || '')) || null;
  };
  let dt = null;
  for (let i = 0; i < 12 && !dt; i++) { dt = await devtoolsTarget(); if (!dt) await sleep(1000); }
  if (dt) {
    log(`   окно DevTools: ${dt.url.slice(0, 80)}`);
    // подключаемся к окну DevTools: своим ws либо через сессию браузера
    let dw = null, dwSess = null;
    if (dt.webSocketDebuggerUrl) dw = await CDP.connect(dt.webSocketDebuggerUrl).catch(() => null);
    if (!dw) {
      try {
        ({ sessionId: dwSess } = await browser.send('Target.attachToTarget',
          { targetId: dt.targetId, flatten: true }, undefined, 5000));
      } catch (e) { log(`   attach окна DevTools: ${e.message}`); }
    }
    if (!dw && !dwSess) { log('   к окну DevTools не подключились'); }
    const evalIn = (expr, ctxId) => (dw
      ? dw.eval(expr, undefined, ctxId, 3000)
      : browser.eval(expr, dwSess, ctxId, 3000));
    const send2 = (m, pr) => (dw ? dw.send(m, pr, undefined, 4000) : browser.send(m, pr, dwSess, 4000));
    const contexts = [];
    const onCtx = (m) => {
      if (m.method === 'Runtime.executionContextCreated') contexts.push(m.params.context);
    };
    if (dw) dw.onEvent = onCtx; else browser.onEvent = onCtx;
    await send2('Runtime.enable', {}).catch((e) => log(`   Runtime.enable: ${e.message}`));
    await send2('Page.enable', {}).catch(() => {});
    await sleep(1500);
    log(`   контекстов DevTools: ${contexts.length}`);

    // панели DevTools и открываем нужную без ввода с клавиатуры
    {
      // без contextId: контекст по умолчанию фронтенда DevTools
      const list = await evalIn(LIST_PANELS).catch((e) => 'ошибка: ' + e.message);
      log(`   панели DevTools: ${list}`);
      const shown = await evalIn(SHOW_PANEL.replace('__EXT_ID__', extIdGuess()))
        .catch((e) => 'ошибка: ' + e.message);
      log(`   ${shown}`);
      await sleep(2000);
      if (/панель открыта/.test(String(shown))) pressed = pressed || false;
    }

    // Панель расширения рисуется в shadow DOM окна DevTools: UI.inspectorView
    // в бандле недоступен, но кнопка #up-save лежит в одной из теней.
    {
      const walkBtn = `(() => {
        const walk = (root) => {
          const b = root.querySelector && root.querySelector('#up-save');
          if (b) return b;
          const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
          for (const el of all) { if (el.shadowRoot) { const r = walk(el.shadowRoot); if (r) return r; } }
          return null;
        };
        const b = walk(document);
        if (!b) {
          return 'в тенях кнопки нет | iframe=' + document.querySelectorAll('iframe').length
            + ' | элементов=' + document.querySelectorAll('*').length
            + ' | title=' + String(document.title).slice(0, 30);
        }
        const t = (b.textContent || '').trim();
        b.click();
        return 'НАЖАТА: "' + t + '"';
      })()`;
      for (let i = 0; i < 8 && !pressed; i++) {
        const r = await evalIn(walkBtn).catch((e) => 'ошибка: ' + e.message);
        log(`   ${r}`);
        if (/НАЖАТА/.test(String(r))) { pressed = true; why = String(r); break; }
        if (!/в тенях кнопки нет/.test(String(r))) break;
        await sleep(500);
      }
    }

    // панель лежит во фрейме внутри окна DevTools — ищем его и жмём кнопку там
    try {
      const tree = await send2('Page.getFrameTree', {}).catch(() => null);
      const frames = [];
      if (tree && tree.frameTree) {
        const walk = (n, d) => {
          frames.push({ id: n.frame.id, url: n.frame.url || '', d });
          (n.childFrames || []).forEach((c) => walk(c, d + 1));
        };
        walk(tree.frameTree, 0);
      }
      log(`   фреймов в DevTools: ${frames.length}`);
      for (const f of frames) log(`      ${'  '.repeat(f.d)}[${f.url.slice(0, 88)}]`);
      // панель может быть отдельной целью (OOPIF) — ищем её среди целей
      if (!frames.some((f) => /\/content\.html/.test(f.url))) {
        await browser.send('Target.setDiscoverTargets', { discover: true });
        const infos = (await browser.send('Target.getTargets')).targetInfos || [];
        const oopif = infos.find((t) => /\/content\.html/.test(t.url || ''));
        if (oopif) {
          log(`   панель отдельной целью: [${oopif.type}] ${oopif.url.slice(0, 70)}`);
          let ow = null, owSess = null;
          if (oopif.webSocketDebuggerUrl) ow = await CDP.connect(oopif.webSocketDebuggerUrl).catch(() => null);
          if (!ow) {
            try {
              ({ sessionId: owSess } = await browser.send('Target.attachToTarget',
                { targetId: oopif.targetId, flatten: true }, undefined, 5000));
            } catch (e) { log(`   attach OOPIF: ${e.message}`); }
          }
          const evalO = (expr, cid) => (ow ? ow.eval(expr, undefined, cid, 3000)
            : browser.eval(expr, owSess, cid, 3000));
          const sendO = (m, pr) => (ow ? ow.send(m, pr, undefined, 4000) : browser.send(m, pr, owSess, 4000));
          const octx = [];
          const onO = (m) => { if (m.method === 'Runtime.executionContextCreated') octx.push(m.params.context); };
          if (ow) ow.onEvent = onO; else browser.onEvent = onO;
          await sendO('Runtime.enable', {}).catch(() => {});
          await sleep(1200);
          log(`   контекстов панели-OOPIF: ${octx.length}`);
          for (const c of octx) {
            const has = await evalO('!!document.getElementById("up-save")', c.id).catch(() => false);
            if (!has) continue;
            why = await evalO(`(() => {
              const b = document.getElementById('up-save');
              const t = (b.textContent || '').trim();
              b.click();
              return 'НАЖАТА: "' + t + '"';
            })()`, c.id).catch((e) => 'ошибка: ' + e.message);
            pressed = /НАЖАТА/.test(why);
            log(`   ${why}`);
            return;
          }
        } else {
          log('   панель не появилась ни фреймом, ни целью');
        }
      }
      const pf = frames.find((f) => /\/content\.html/.test(f.url));
      if (pf) {
        const w = await send2('Page.createIsolatedWorld',
          { frameId: pf.id, worldName: 'rs', grantUniveralAccess: true }).catch(() => null);
        if (w && w.executionContextId) {
          log(`   контекст панели: ${w.executionContextId}`);
          const has = await evalIn('!!document.getElementById("up-save")', w.executionContextId)
            .catch(() => false);
          if (has) {
            why = await evalIn(`(() => {
              const b = document.getElementById('up-save');
              const t = (b.textContent || '').trim();
              b.click();
              return 'НАЖАТА: "' + t + '"';
            })()`, w.executionContextId).catch((e) => 'ошибка: ' + e.message);
            pressed = /НАЖАТА/.test(why);
            log(`   ${why}`);
            return;
          }
          log('   в панели кнопки #up-save нет');
        }
      }
    } catch (e) { log(`   фреймы DevTools: ${e.message}`); }
    for (const c of contexts) {
      const has = await evalIn('!!document.getElementById("up-save")', c.id).catch(() => false);
      if (!has) continue;
      why = await evalIn(`(() => {
        const b = document.getElementById('up-save');
        const t = (b.textContent || '').trim();
        b.click();
        return 'НАЖАТА: "' + t + '"';
      })()`, c.id).catch((e) => 'ошибка: ' + e.message);
      pressed = /НАЖАТА/.test(why);
      log(`   ${why}`);
      break;
    }
  } else {
    log('   окно DevTools среди целей не найдено');
  }

  // все окна DevTools подряд: нужное определяется по загруженному фронтенду
  if (!pressed) {
    const r = await pressInAnyDevtoolsWindow(browser, devtools);
    log(`   окна DevTools: ${r}`);
    if (/НАЖАТА/.test(String(r))) { pressed = true; why = String(r); }
  }

  const extId = process.env.EXT_ID || unpackedExtensionId(EXT_DIR);

  // 1) реальные ресурсы страницы — их панель заберёт через подмену chrome.devtools
  const collected = await collectFromGame(browser);
  const resources = [...collected.bodies].map(([url, v]) => ({ url, body: v.body, size: v.size }));
  const har = resources.map((r) => ({
    request: { url: r.url, method: 'GET' },
    response: { status: 200, content: { size: r.size, mimeType: 'text/plain' } },
  }));
  const shim = SHIM_SOURCE
    .replace('__RESOURCES__', JSON.stringify(resources))
    .replace('__HAR__', JSON.stringify(har));
  log(`   в шэм пойдёт ресурсов: ${resources.length}`);

  let clicked = pressed ? why
    : (extId ? await pressViaIframe(browser, extId, shim) : 'EXT_ID не задан');
  why = String(clicked);
  log(`   панель в iframe: ${why}`);
  if (/НАЖАТА/.test(why)) pressed = true; else why = why;
  if (!pressed && extId) {
    why = String(await pressViaPanelTab(browser, extId, shim));
    log(`   панель во вкладке: ${why}`);
    if (/НАЖАТА/.test(why)) pressed = true;
  }
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
    console.error('кнопка не нажата');
    console.error(`причина: ${why}`);
    process.exit(1);
  }
  log(`кнопка нажата (${since()}) — архив собирает отдельный шаг`);
  process.exit(0);
}

main().catch((e) => { console.error('ошибка:', e.message); process.exit(1); });
