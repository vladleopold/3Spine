#!/usr/bin/env node
// Загружаем распакованное расширение через настоящий интерфейс chrome://extensions.
// Причина: в Chrome 153 на ubuntu-latest расширение не ставится ни флагом
// --load-extension, ни через CDP-домен Extensions (chrome://extensions пуст,
// панели chrome.devtools.panels.create в DevTools не появляется).
// Поэтому нажимаем настоящей мышью Developer mode → Load unpacked и печатаем
// путь в системном диалоге выбора папки.
import fs from 'fs';
import path from 'path';
import { execSync } from 'child_process';

const EXT_DIR = path.resolve(process.env.EXT_DIR || './.chrome-ext');
const PROFILE_DIR = path.resolve(process.env.PROFILE || './.chrome-profile');
const PORT = parseInt(process.env.CDP_PORT || '9222', 10);
const DISPLAY = process.env.DISPLAY || ':99';

const t0 = Date.now();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(...a);
const left = () => 90000 - (Date.now() - t0);

const xdo = (cmd) => {
  try {
    return execSync(`xdotool ${cmd}`, {
      env: { ...process.env, DISPLAY }, timeout: 8000,
    }).toString().trim();
  } catch { return ''; }
};

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

  send(method, params = {}, sessionId, timeoutMs = 8000) {
    const id = ++this.n;
    const msg = { id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    this.ws.send(JSON.stringify(msg));
    return new Promise((res, rej) => {
      const timer = setTimeout(() => { this.waiting.delete(id); rej(new Error(`${method}: нет ответа`)); }, timeoutMs);
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

// реальная мышь в окне страницы: DevTools/Chrome реагируют на mousedown
async function realClick(c, sid, x, y) {
  for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased']) {
    await c.send('Input.dispatchMouseEvent', {
      type, x: Math.round(x), y: Math.round(y),
      button: 'left', buttons: type === 'mousePressed' ? 1 : 0, clickCount: 1,
    }, sid, 4000).catch(() => {});
  }
}

const WALK = `(() => {
  const roots = [document];
  const scan = (r) => {
    const all = r.querySelectorAll ? r.querySelectorAll('*') : [];
    for (const el of all) if (el.shadowRoot) { roots.push(el.shadowRoot); scan(el.shadowRoot); }
  };
  scan(document);
  return roots;
})()`;

async function main() {
  if (!fs.existsSync(path.join(EXT_DIR, 'manifest.json'))) {
    throw new Error(`нет расширения в ${EXT_DIR}`);
  }
  const v = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json();
  const browser = await CDP.connect(v.webSocketDebuggerUrl);
  log('Chrome подключён по CDP');

  // 1) вкладка со страницей расширений
  const { targetId } = await browser.send('Target.createTarget', { url: 'chrome://extensions/' }, undefined, 8000);
  const { sessionId: sid } = await browser.send('Target.attachToTarget',
    { targetId, flatten: true }, undefined, 8000);
  const ctxs = [];
  browser.onEvent = (m) => {
    if (m.sessionId === sid && m.method === 'Runtime.executionContextCreated') ctxs.push(m.params.context);
  };
  await browser.send('Runtime.enable', {}, sid, 5000);
  await sleep(2500);
  const cx = ctxs.find((c) => c.auxData && c.auxData.isDefault) || ctxs[0];
  if (!cx) throw new Error('контекст chrome://extensions не появился');
  log(`   страница расширений открыта, контекстов: ${ctxs.length}`);

  // 2) включаем Developer mode
  const devRect = await browser.eval(`(() => {
    const roots = ${WALK};
    for (const r of roots) for (const el of r.querySelectorAll('*')) {
      const id = el.id || '';
      if (id !== 'devMode' && !/devMode/i.test(id)) continue;
      const b = el.getBoundingClientRect();
      if (b.width < 4 || b.height < 4) continue;
      return { x: b.left + b.width / 2, y: b.top + b.height / 2, on: el.checked === true || el.getAttribute('aria-pressed') === 'true' };
    }
    return null;
  })()`, sid, cx.id).catch((e) => null);
  if (devRect && !devRect.on) {
    log(`   включаю Developer mode в точке ${Math.round(devRect.x)},${Math.round(devRect.y)}`);
    await realClick(browser, sid, devRect.x, devRect.y);
    await sleep(1500);
  } else if (devRect) {
    log('   Developer mode уже включён');
  } else {
    log('   переключатель Developer mode не найден');
  }

  // 3) жмём Load unpacked
  const loadRect = await browser.eval(`(() => {
    const roots = ${WALK};
    for (const r of roots) for (const el of r.querySelectorAll('*')) {
      const id = String(el.id || '');
      const txt = (el.innerText || el.textContent || '').trim();
      const isLoad = id === 'loadUnpacked' || /load unpacked|загрузить распакован/i.test(txt);
      if (!isLoad) continue;
      if (el.tagName === 'CR-BUTTON' || el.tagName === 'BUTTON' || el.tagName === 'CR-ICON-BUTTON' || /^(DIV|SPAN)$/.test(el.tagName)) {
        const b = el.getBoundingClientRect();
        if (b.width < 6 || b.height < 6) continue;
        if (el.tagName !== 'CR-BUTTON' && el.tagName !== 'BUTTON' && !/^(DIV|SPAN)$/.test(el.tagName)) continue;
        return { x: b.left + b.width / 2, y: b.top + b.height / 2, tag: el.tagName, id: id || '(без id)' };
      }
    }
    return null;
  })()`, sid, cx.id).catch(() => null);
  if (!loadRect) {
    log('   кнопка Load unpacked не найдена');
    log(await browser.eval(`(() => {
      const roots = ${WALK};
      const out = [];
      for (const r of roots) for (const el of r.querySelectorAll('button, cr-button, [role=button]')) {
        const t = (el.innerText || el.textContent || '').trim();
        if (t) out.push(el.tagName + '#' + (el.id || '-') + '=' + t.slice(0, 24));
      }
      return 'кнопки: ' + out.join(' ; ').slice(0, 300);
    })()`, sid, cx.id).catch((e) => 'ошибка: ' + e.message));
    return;
  }
  log(`   жму Load unpacked (<${loadRect.tag} id=${loadRect.id}>) в точке ${Math.round(loadRect.x)},${Math.round(loadRect.y)})`);
  await realClick(browser, sid, loadRect.x, loadRect.y);
  await sleep(3000);

  // 4) системный диалог выбора папки: печатаем путь
  const win = xdo('search --onlyvisible --class "chrome|google-chrome" | tail -1');
  if (win) xdo(`windowactivate --sync ${win}`);
  await sleep(1000);
  xdo('key --clearmodifiers ctrl+l');
  await sleep(500);
  xdo(`type --delay 40 ${JSON.stringify(EXT_DIR)}`);
  await sleep(800);
  xdo('key --clearmodifiers Return');
  log(`   путь отправлен в диалог: ${EXT_DIR}`);
  await sleep(4000);

  // 5) проверяем результат
  const installed = await browser.eval(`(() => {
    const roots = ${WALK};
    const items = [];
    for (const r of roots) for (const el of r.querySelectorAll('*')) {
      if (!/extensions-item/.test(String(el.tagName || ''))) continue;
      items.push((el.id || '?') + (el.hasAttribute('disabled') ? ' ВЫКЛЮЧЕНО' : ' включено'));
    }
    return items.join(' , ') || 'ни одного';
  })()`, sid, cx.id).catch((e) => 'ошибка: ' + e.message);
  log(`   расширений на странице: ${installed}`);

  // 6) возвращаем игровую вкладку вперёд и закрываем страницу расширений
  await browser.send('Target.closeTarget', { targetId }, undefined, 4000).catch(() => {});
  const pages = (await browser.send('Target.getTargets').catch(() => ({ targetInfos: [] }))).targetInfos || [];
  const game = pages.find((t) => t.type === 'page' && /^https?:/.test(t.url || ''));
  if (game) {
    await browser.send('Target.activateTarget', { targetId: game.targetId }, undefined, 4000).catch(() => {});
  }
  log(`   готово за ${((Date.now() - t0) / 1000).toFixed(1)}с`);
}

main().catch((e) => { console.error('ошибка загрузки расширения:', e.message); process.exit(1); });
