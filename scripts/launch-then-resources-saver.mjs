#!/usr/bin/env node
// Порядок ровно как заказано:
//   1) открываем Chrome и ссылку (без расширения)
//   2) "устанавливаем" Resources-Saver (файлы уже на диске)
//   3) перезапускаем Chrome с расширением, та же сессия и та же ссылка
//   4) запускаем Resources-Saver в уже открытом Chrome: открываем его popup
//      и нажимаем кнопку сохранения
import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';

const URL_ = process.env.URL || 'https://example.com';
const OUT = process.env.OUTPUT_DIR || './rs-out';
const EXT = path.resolve(process.env.EXT_DIR || './.chrome-ext');
const PROFILE = process.env.PROFILE || path.join(OUT, '.profile');
const CHROME = process.env.CHROME_PATH
  || (fs.existsSync('/usr/bin/google-chrome') ? '/usr/bin/google-chrome' : '');
const WAIT = parseInt(process.env.WAIT_MS || '20000', 10);
const EXT_WAIT = parseInt(process.env.EXT_WAIT_MS || '12000', 10);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(...a);
const base = (extra = []) => [
  '--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--no-first-run',
  '--disable-blink-features=AutomationControlled', ...extra,
];
const ctxOpts = (extra = []) => ({
  headless: false,   // расширения не работают в --headless=old
  executablePath: CHROME || undefined,
  ignoreHTTPSErrors: true,
  acceptDownloads: true,
  downloadsPath: OUT,
  args: base(extra),
});

async function openWith(page) {
  await page.goto(URL_, { waitUntil: 'domcontentloaded', timeout: 45000 });
  log(`   страница: ${page.url()}`);
  log(`   заголовок: ${(await page.title().catch(() => '')) || '—'}`);
}

async function extId(ctx) {
  for (let i = 0; i * 500 < EXT_WAIT; i++) {
    const sw = ctx.serviceWorkers()[0];
    if (sw) return new URL(sw.url()).host;
    const bg = ctx.backgroundPages()[0];
    if (bg) return new URL(bg.url()).host;
    await sleep(500);
  }
  return '';
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  fs.mkdirSync(PROFILE, { recursive: true });
  log(`ссылка: ${URL_}`);
  log(`chrome: ${CHROME || 'системный'}`);

  // 1) Chrome + ссылка, расширения ещё нет
  log('1) открываю Chrome и ссылку…');
  let ctx = await chromium.launchPersistentContext(PROFILE, ctxOpts());
  let page = ctx.pages()[0] || await ctx.newPage();
  const cdp = await ctx.newCDPSession(page);
  await cdp.send('Browser.setDownloadBehavior',
    { behavior: 'allow', downloadPath: OUT, eventsEnabled: true });
  await openWith(page);
  await sleep(Math.min(WAIT, 8000));
  log('   ссылка открыта, закрываю Chrome (это и будет перезапуск)…');
  await ctx.close();

  // 2) расширение "устанавливаем" — проверяем, что оно на диске
  const mPath = path.join(EXT, 'manifest.json');
  if (!fs.existsSync(mPath)) throw new Error(`расширение не установлено: ${mPath}`);
  const m = JSON.parse(fs.readFileSync(mPath, 'utf8'));
  log(`2) расширение установлено: ${m.name} v${m.version}`);

  // 3) перезапуск Chrome с расширением, та же сессия и та же ссылка
  log('3) перезапускаю Chrome с расширением…');
  ctx = await chromium.launchPersistentContext(PROFILE, ctxOpts([
    `--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`,
  ]));
  page = ctx.pages()[0] || await ctx.newPage();
  const cdp2 = await ctx.newCDPSession(page);
  await cdp2.send('Browser.setDownloadBehavior',
    { behavior: 'allow', downloadPath: OUT, eventsEnabled: true });
  await openWith(page);
  const id = await extId(ctx);
  if (!id) {
    await ctx.close();
    throw new Error('расширение не активировалось после перезапуска');
  }
  log(`   расширение активно: chrome-extension://${id}/`);
  await sleep(3000);

  // 4) запускаем Resources-Saver в уже открытом Chrome
  const popupFile = (m.action && m.action.default_popup) || m.devtools_page || 'popup.html';
  const pop = await ctx.newPage();
  await pop.goto(`chrome-extension://${id}/${popupFile}`, { waitUntil: 'domcontentloaded' });
  const btns = await pop.$$eval('button, input[type=button], input[type=submit], a[role=button]',
    (els) => els.map((e, i) => ({
      i,
      text: (e.innerText || e.value || '').trim().slice(0, 40),
      id: e.id || '', cls: (e.className || '').toString().slice(0, 40),
    })));
  log(`   кнопок в popup: ${btns.length}` + (btns.length ? ` → ${JSON.stringify(btns.slice(0, 5))}` : ''));
  const want = /save|download|zip|скач|сохран|resources/i;
  const pick = btns.find((b) => want.test(`${b.text} ${b.id} ${b.cls}`)) || btns[0];
  if (!pick) {
    await ctx.close();
    throw new Error('в popup расширения нет кнопки запуска');
  }
  log(`4) нажимаю кнопку расширения: "${pick.text || pick.id || pick.cls}"`);
  await pop.click('button, input[type=button], input[type=submit], a[role=button]',
    { position: { x: 5, y: 5 }, timeout: 5000 }).catch(async () => {
      await pop.locator('button').first().click({ timeout: 5000 });
    });

  // ждём ZIP
  const deadline = Date.now() + 120000;
  let zip = null;
  while (Date.now() < deadline) {
    const z = fs.readdirSync(OUT).filter((f) => f.endsWith('.zip') && !f.endsWith('.crdownload'));
    if (z.length) { zip = z[z.length - 1]; break; }
    await sleep(2000);
  }
  if (!zip) {
    const txt = (await pop.content().catch(() => '')).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').slice(0, 200);
    log(`   popup после клика: ${txt}`);
  }
  await ctx.close();
  if (!zip) { log('архив не появился'); process.exit(2); }
  log(`готово: ${path.join(OUT, zip)} (${(fs.statSync(path.join(OUT, zip)).size / 1048576).toFixed(1)} МБ)`);
}

main().catch((e) => { console.error('ошибка:', e.message); process.exit(1); });
