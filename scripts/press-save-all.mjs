#!/usr/bin/env node
// Нажимаем «Save All Resources» в панели Resources Saver.
//
// Проблема: окно DevTools не появляется среди целей CDP (Chrome не отдаёт
// фронтенд DevTools как target), поэтому кликнуть по нему через CDP нельзя.
//
// Решение: открываем ту же панель расширения (chrome-extension://<id>/content.html)
// как страницу и подставляем ей chrome.devtools через подмену: ресурсы и HAR
// собираем сами через CDP. Кнопка #up-save — настоящая, из content.html.
import { chromium } from 'playwright';
import crypto from 'crypto';
import fs from 'fs';
import path from 'path';

const URL_ = process.env.URL || '';
const OUT = path.resolve(process.env.OUTPUT_DIR || './artifacts');
const EXT = path.resolve(process.env.EXT_DIR || './.chrome-ext');
const PORT = parseInt(process.env.CDP_PORT || '9222', 10);
const COLLECT_MS = parseInt(process.env.COLLECT_MS || '20000', 10);
const ZIP_TIMEOUT = parseInt(process.env.ZIP_TIMEOUT_MS || '240000', 10);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(...a);

function unpackedExtensionId(dir) {
  const h = crypto.createHash('sha256').update(dir).digest('hex').slice(0, 32);
  return h.split('').map((c) => String.fromCharCode(97 + parseInt(c, 16))).join('');
}

// id нашего расширения: в chrome://extensions-internals есть запись с путём,
// по которому мы грузили расширение. Цели Chrome тут ненадёжны — среди них
// бывают встроенные компонентные расширения без popup.html.
// Chrome 137+ не грузит расширение через --load-extension (ERR_BLOCKED_BY_CLIENT).
// Поддерживаемый путь — CDP-домен Extensions.loadUnpacked; он же возвращает id.
async function loadUnpacked(port, extPath) {
  const v = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
  const ws = new WebSocket(v.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener('open', res, { once: true });
    ws.addEventListener('error', () => rej(new Error('CDP браузера недоступен')), { once: true });
  });
  const answer = new Promise((res) => {
    ws.addEventListener('message', (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.id === 1) res(m);
    });
  });
  ws.send(JSON.stringify({
    id: 1, method: 'Extensions.loadUnpacked', params: { path: extPath },
  }));
  const m = await Promise.race([
    answer,
    new Promise((res) => setTimeout(() => res({ error: { message: 'таймаут' } }), 20000)),
  ]);
  ws.close();
  if (m.error) throw new Error(`Extensions.loadUnpacked: ${m.error.message}`);
  return (m.result && m.result.id) || '';
}

async function dumpExtensions(ctx) {
  const p = await ctx.newPage();
  try {
    await p.goto('chrome://extensions-internals/', { waitUntil: 'domcontentloaded', timeout: 15000 });
    const txt = (await p.textContent('body').catch(() => '')) || '';
    let data = [];
    try { data = JSON.parse(txt); } catch { /* не JSON */ }
    log(`ВСЕ расширения в профиле: ${Array.isArray(data) ? data.length : 'не JSON'}`);
    for (const e of (Array.isArray(data) ? data : [])) {
      const nm = e.name || (e.manifest && e.manifest.name) || '?';
      log(`   id=${e.id} имя="${nm}" путь=${e.path || e.manifest_path || '—'}`);
    }
    if (!Array.isArray(data)) log(`   сырой ответ: ${txt.slice(0, 300)}`);
  } catch (e) {
    log(`chrome://extensions-internals недоступна: ${e.message.split('\n')[0]}`);
  } finally {
    await p.close().catch(() => {});
  }
}

async function extensionIdFromProfile(ctx, extDir) {
  const p = await ctx.newPage();
  let txt = '';
  try {
    await p.goto('chrome://extensions-internals/', { waitUntil: 'domcontentloaded', timeout: 15000 });
    txt = (await p.textContent('body').catch(() => '')) || '';
  } catch { /* страница недоступна */ }
  await p.close().catch(() => {});
  let data = [];
  try { data = JSON.parse(txt); } catch { /* не JSON */ }
  if (!Array.isArray(data)) data = [];
  const dir = extDir.replace(/\/$/, '');
  for (const e of data) {
    const where = `${e.path || ''} ${e.manifest_path || ''} ${e.install_path || ''}`;
    if (where.includes(dir)) return e.id;
  }
  for (const e of data) {
    const nm = e.name || (e.manifest && e.manifest.name) || '';
    if (/resources saver/i.test(nm)) return e.id;
  }
  return '';
}

// Запасной путь: Chrome сам сообщает id в целях (service worker расширения).
// Вычисленный по пути id может не совпасть — тогда страница панели отдаёт
// ERR_BLOCKED_BY_CLIENT.
async function realExtensionId(ctx, port, extDir) {
  for (let i = 0; i * 500 < 10000; i++) {
    try {
      const ts = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      const t = ts.find((x) => (x.url || '').startsWith('chrome-extension://'));
      if (t) return new URL(t.url).host;
    } catch { /* список целей недоступен */ }
    // поднимаем service worker расширения, чтобы он появился в целях
    if (i === 2) {
      const p = await ctx.newPage().catch(() => null);
      if (p) { await p.goto('chrome://extensions/').catch(() => {}); await p.close().catch(() => {}); }
    }
    await sleep(500);
  }
  return unpackedExtensionId(extDir);
}

const SHIM = (payload) => `(() => {
  const DATA = ${payload};
  const noop = { addListener() {}, removeListener() {} };
  const resources = DATA.resources.map((r) => ({ url: r.url, content: r.body, size: r.size }));
  chrome.devtools = {
    inspectedWindow: {
      tabId: DATA.tabId,
      getResources(cb) { cb(resources); },
      onResourceAdded: noop,
      reload() {},
      eval() {},
    },
    network: {
      getHAR(cb) { cb({ log: { version: '1.2', entries: DATA.har } }); },
      onRequestFinished: noop,
    },
  };
})();`;

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const m = JSON.parse(fs.readFileSync(path.join(EXT, 'manifest.json'), 'utf8'));
  log(`расширение: ${m.name} v${m.version}`);

  // Chrome уже запущен предыдущим шагом (с расширением и ссылкой) — подключаемся к нему
  let browser;
  for (let i = 0; i * 500 < 20000; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (r.ok) break;
    } catch { /* порт ещё не поднят */ }
    await sleep(500);
  }
  browser = await chromium.connectOverCDP(`http://127.0.0.1:${PORT}`);
  log('подключился к уже запущенному Chrome');
  const ctx = browser.contexts()[0];

  // 1) собираем все ресурсы страницы через CDP
  const page = ctx.pages()[0] || await ctx.newPage();
  const cdp = await ctx.newCDPSession(page);
  await cdp.send('Browser.setDownloadBehavior',
    { behavior: 'allow', downloadPath: OUT, eventsEnabled: true });
  await cdp.send('Network.enable');

  const bodies = new Map();     // url -> { body, mimeType, size }
  cdp.on('Network.responseReceived', async (ev) => {
    const { response, requestId } = ev;
    if (!/^https?:/i.test(response.url)) return;
    if (/cdn-cgi\/challenge|googletagmanager|google-analytics|ipify/i.test(response.url)) return;
    try {
      const r = await cdp.send('Network.getResponseBody', { requestId });
      const body = r.base64Encoded ? Buffer.from(r.body, 'base64').toString('utf8') : r.body;
      bodies.set(response.url, {
        body, size: (body || '').length, mimeType: response.mimeType || 'text/plain',
      });
    } catch { /* тело уже вытеснено из кеша */ }
  });

  if (URL_ && !page.url().startsWith(URL_.split('?')[0])) {
    log(`открываю ${URL_}`);
    await page.goto(URL_, { waitUntil: 'domcontentloaded', timeout: 45000 });
  }
  // перезагрузка с включённой сетью — чтобы поймать всё, что игра грузит сама
  await page.reload({ waitUntil: 'domcontentloaded' }).catch(() => {});
  await sleep(Math.min(COLLECT_MS, 20000));
  log(`ресурсов собрано: ${bodies.size}`);

  const resources = [...bodies].map(([url, v]) => ({ url, body: v.body, size: v.size }));
  const har = resources.map((r) => {
    const v = bodies.get(r.url);
    return { request: { url: r.url, method: 'GET' },
      response: { status: 200, content: { size: v.size, mimeType: v.mimeType } } };
  });

  // 2) перебираем известные id: из профиля, вычисленный по пути, из целей.
  //    Берём тот, у которого панель реально открывается — так не зависим от
  //    того, откуда взялся id (цели Chrome могут принадлежать чужим расширениям).
  // сначала пробуем официальную загрузку через CDP — она же даёт id
  let loaded = '';
  try {
    loaded = await loadUnpacked(PORT, EXT);
    log(`Extensions.loadUnpacked: ${loaded ? 'ok, id ' + loaded : 'без id'}`);
  } catch (e) {
    log(`Extensions.loadUnpacked не сработал: ${e.message}`);
  }
  const fromProfile = await extensionIdFromProfile(ctx, EXT);
  const fromTargets = await realExtensionId(ctx, PORT, EXT);
  const computed = unpackedExtensionId(EXT);
  const candidates = [...new Set([loaded, fromProfile, computed, fromTargets].filter(Boolean))];
  log(`кандидаты id: ${candidates.join(', ') || 'нет'}`);

  let extId = '';
  let panel = null;
  for (const id of candidates) {
    const test = await ctx.newPage();
    try {
      await test.goto(`chrome-extension://${id}/content.html`, { waitUntil: 'domcontentloaded', timeout: 15000 });
      extId = id;
      panel = test;
      log(`панель открылась с id ${id}`);
      break;
    } catch (e) {
      log(`   id ${id}: ${e.message.split('\n')[0].slice(0, 90)}`);
      await test.close().catch(() => {});
    }
  }
  if (!extId) throw new Error('ни один id не открыл панель Resources Saver');

  // 3) настоящий id вкладки игры: content.js берёт список сайтов через
  //    chrome.tabs.get(chrome.devtools.inspectedWindow.tabId) — с фиктивным id
  //    список был пуст и кнопка ничего не качала
  const tabs = await panel.evaluate(() => new Promise((res) => {
    chrome.tabs.query({}, (list) => res((list || []).map((x) => ({ id: x.id, url: x.url || '' }))));
  })).catch(() => []);
  const origin = (() => { try { return new URL(URL_ || page.url()).origin; } catch { return ''; } })();
  const gameTab = tabs.find((t) => t.url.startsWith(origin)) || tabs[0];
  const tabId = gameTab ? gameTab.id : 0;
  log(`вкладка игры: id=${tabId} ${gameTab ? gameTab.url.slice(0, 70) : '—'}`);

  // 4) подменяем панели chrome.devtools собранными ресурсами и жмём кнопку
  await panel.addInitScript(SHIM(JSON.stringify({ resources, har, tabId })));
  await panel.reload({ waitUntil: 'domcontentloaded' });
  const btn = panel.locator('#up-save');
  await btn.waitFor({ state: 'visible', timeout: 20000 });
  const label = (await btn.textContent().catch(() => '')) || '';
  log(`нажимаю кнопку: "${label.trim()}"`);
  await panel.evaluate(() => document.getElementById('up-save').click());

  // 5) ждём ZIP
  const deadline = Date.now() + ZIP_TIMEOUT;
  let zip = null;
  while (Date.now() < deadline) {
    const z = fs.readdirSync(OUT).filter((f) => f.endsWith('.zip') && !f.endsWith('.crdownload'));
    if (z.length) { zip = z[z.length - 1]; break; }
    await sleep(2000);
  }
  if (!zip) {
    const state = (await panel.textContent('body').catch(() => '')).replace(/\s+/g, ' ').slice(0, 300);
    log(`состояние панели: ${state}`);
    await browser.close().catch(() => {});
    throw new Error('ZIP не появился после нажатия Save All Resources');
  }
  log(`готово: ${path.join(OUT, zip)} (${(fs.statSync(path.join(OUT, zip)).size / 1048576).toFixed(1)} МБ)`);
  await browser.close().catch(() => {});
}

main().catch((e) => { console.error('ошибка:', e.message); process.exit(1); });
