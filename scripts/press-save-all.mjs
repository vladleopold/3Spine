#!/usr/bin/env node
// Шаг «Нажать Save All Resources»: панель Resources Saver в DevTools, клик кнопки,
// ожидание архива. Лимит шага — 30 секунд (TOTAL_LIMIT_MS).
import fs from 'fs';
import path from 'path';
import { execSync } from 'child_process';

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

async function pressSaveWithMouse() {
  // 1) отдельное окно DevTools
  const devWin = xdo('search --onlyvisible --name "DevTools" | tail -1');
  if (devWin) {
    xdo(`windowactivate --sync ${devWin}`);
    const g = geometry(devWin);
    log(`   окно DevTools: ${devWin} ${g.width}x${g.height}`);
    // кнопка «Save All Resources» — в шапке панели, слева сверху
    xdo(`mousemove --window ${devWin} 120 30 click 1`);
    return 'клик мышью по кнопке в окне DevTools';
  }
  // 2) DevTools пристыкован — кликаем в области панели окна Chrome
  const win = xdo('search --onlyvisible --class "google-chrome" | tail -1')
    || xdo('search --onlyvisible --name "Chrome" | tail -1');
  if (!win) return 'окно Chrome не найдено';
  const g = geometry(win);
  const w = parseInt(g.width || '1500', 10);
  const h = parseInt(g.height || '950', 10);
  xdo(`windowactivate --sync ${win}`);
  // док внизу: панель начинается на ~35% снизу; док справа: панель в правой части
  const spots = [
    [Math.round(w * 0.08), Math.round(h * 0.62)],
    [Math.round(w * 0.08), Math.round(h * 0.42)],
    [Math.round(w * 0.86), Math.round(h * 0.42)],
    [Math.round(w * 0.08), Math.round(h * 0.18)],
  ];
  for (const [dx, dy] of spots) {
    xdo(`mousemove --window ${win} ${dx} ${dy} click 1`);
    await sleep(700);
  }
  return `клики по панели в пристыкованном DevTools (окно ${w}x${h})`;
}

function pressSaveWithKeyboard() {
  const win = xdo('search --onlyvisible --class "google-chrome" | tail -1')
    || xdo('search --onlyvisible --name "Chrome" | tail -1');
  if (!win) return 'окно Chrome не найдено';
  xdo(`windowactivate --sync ${win}`);
  // первый фокусируемый элемент панели — как раз кнопка сохранения
  for (let i = 0; i < 3; i++) {
    xdo(`key --window ${win} Tab`);
    xdo(`key --window ${win} Return`);
  }
  return 'Tab+Enter в панели';
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

  // 0) сначала настоящее нажатие вводом X11 — окно DevTools недоступно по CDP
  let clicked = null;
  try {
    const how = pressSaveWithMouse();
    log(`   ${how}`);
    const kb = pressSaveWithKeyboard();
    log(`   ${kb}`);
  } catch (e) {
    log(`   ввод X11 не сработал: ${e.message}`);
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
  if (!clicked) log('   кнопка нажата вводом X11 (CDP-путь недоступен)');

  // 4) ждём ZIP в остатке лимита (после нажатия X11 архив собирается ~15 с)
  let dl = Date.now() + Math.min(ZIP_TIMEOUT, Math.max(2000, left()));
  let zip = null;
  while (Date.now() < dl) {
    const z = fs.readdirSync(OUT).filter((f) => f.endsWith('.zip') && !f.endsWith('.crdownload'));
    if (z.length) { zip = z[z.length - 1]; break; }
    await sleep(1000);
  }
  if (!zip && left() > 4000) {
    // ещё одно нажатие X11 — панель могла быть не в фокусе
    log('   ZIP пока нет, повторяю нажатие');
    try {
      log(`   ${await pressSaveWithMouse()}`);
      log(`   ${pressSaveWithKeyboard()}`);
    } catch (e) { log(`   повтор не сработал: ${e.message}`); }
    dl = Date.now() + Math.max(2000, left());
    while (Date.now() < dl) {
      const z = fs.readdirSync(OUT).filter((f) => f.endsWith('.zip') && !f.endsWith('.crdownload'));
      if (z.length) { zip = z[z.length - 1]; break; }
      await sleep(1000);
    }
  }

  if (!zip) {
    // запасной путь: собираем ресурсы сами и повторяем нажатие
    log('нажатие не дало архива, пробую запасной путь: сбор ресурсов и повторное нажатие');
    await collectResources();
    if (!pc) { log('   панель недоступна по CDP, повторное нажатие только вводом X11'); }
    const again = pc ? await pc.eval(`(() => {
      const b = document.getElementById('up-save');
      if (!b) return 'кнопки нет';
      b.click();
      return 'повторно нажата';
    })()`).catch((e) => 'ошибка: ' + e.message) : 'пропущено';
    log(`   ${again} (${since()})`);
    const dl2 = Date.now() + Math.max(2000, left());
    while (Date.now() < dl2) {
      const z = fs.readdirSync(OUT).filter((f) => f.endsWith('.zip') && !f.endsWith('.crdownload'));
      if (z.length) { zip = z[z.length - 1]; break; }
      await sleep(1000);
    }
  }
  if (!zip) {
    // главный вывод: кнопка не нажата (окно не активировалось) — архив не начи��ался
    throw new Error(`кнопка Save All Resources не нажата — архив не создавался (${since()})`);
  }
  log(`готово: ${path.join(OUT, zip)} (${(fs.statSync(path.join(OUT, zip)).size / 1048576).toFixed(1)} МБ) за ${since()}`);
  process.exit(0);
}

main().catch((e) => { console.error('ошибка:', e.message); process.exit(1); });
