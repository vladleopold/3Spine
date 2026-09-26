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
const PROFILE = path.resolve(process.env.PROFILE || './.chrome-profile');
const CHROME = process.env.CHROME_PATH
  || (fs.existsSync('/usr/bin/google-chrome') ? '/usr/bin/google-chrome' : '');
const COLLECT_MS = parseInt(process.env.COLLECT_MS || '20000', 10);
const ZIP_TIMEOUT = parseInt(process.env.ZIP_TIMEOUT_MS || '240000', 10);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(...a);

function unpackedExtensionId(dir) {
  const h = crypto.createHash('sha256').update(dir).digest('hex').slice(0, 32);
  return h.split('').map((c) => String.fromCharCode(97 + parseInt(c, 16))).join('');
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
  fs.mkdirSync(PROFILE, { recursive: true });
  const m = JSON.parse(fs.readFileSync(path.join(EXT, 'manifest.json'), 'utf8'));
  const extId = unpackedExtensionId(EXT);
  log(`расширение: ${m.name} v${m.version}, id ${extId}`);

  const ctx = await chromium.launchPersistentContext(PROFILE, {
    headless: false,
    executablePath: CHROME || undefined,
    ignoreHTTPSErrors: true,
    acceptDownloads: true,
    args: [
      '--no-sandbox', '--disable-dev-shm-usage', '--no-first-run',
      '--disable-blink-features=AutomationControlled',
      `--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`,
    ],
  });

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

  log(`открываю ${URL_}`);
  await page.goto(URL_, { waitUntil: 'domcontentloaded', timeout: 45000 });
  await sleep(Math.min(COLLECT_MS, 20000));
  log(`ресурсов собрано: ${bodies.size}`);

  const resources = [...bodies].map(([url, v]) => ({ url, body: v.body, size: v.size }));
  const har = resources.map((r) => {
    const v = bodies.get(r.url);
    return { request: { url: r.url, method: 'GET' },
      response: { status: 200, content: { size: v.size, mimeType: v.mimeType } } };
  });

  // 2) открываем панель Resources Saver и подменяем ей chrome.devtools
  const panel = await ctx.newPage();
  await panel.addInitScript(SHIM(JSON.stringify({
    resources, har, tabId: 9001,
  })));
  const url = `chrome-extension://${extId}/content.html`;
  await panel.goto(url, { waitUntil: 'domcontentloaded' });
  log(`панель Resources Saver открыта: ${url}`);

  const btn = panel.locator('#up-save');
  await btn.waitFor({ state: 'visible', timeout: 20000 });
  const label = (await btn.textContent().catch(() => '')) || '';
  log(`нажимаю кнопку: "${label.trim()}"`);
  await panel.evaluate(() => document.getElementById('up-save').click());

  // 3) ждём ZIP
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
    await ctx.close();
    throw new Error('ZIP не появился после нажатия Save All Resources');
  }
  log(`готово: ${path.join(OUT, zip)} (${(fs.statSync(path.join(OUT, zip)).size / 1048576).toFixed(1)} МБ)`);
  await ctx.close();
}

main().catch((e) => { console.error('ошибка:', e.message); process.exit(1); });
