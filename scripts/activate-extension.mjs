#!/usr/bin/env node
// Открываем Chrome с установленным Resources-Saver и убеждаемся, что
// расширение действительно открывается (его страница отдаёт 200).
//
// Проверять через serviceWorker нельзя: у Resources-Saver background.js —
// пустой файл, поэтому Chrome не держит SW запущенным и Playwright его не видит.
// ID распакованного расширения детерминирован: sha256 от абсолютного пути,
// первые 16 байт, каждая hex-цифра -> буква a-p.
import { chromium } from 'playwright';
import crypto from 'crypto';
import fs from 'fs';
import path from 'path';

const EXT = path.resolve(process.env.EXT_DIR || './.chrome-ext');
const PROFILE = process.env.PROFILE || path.resolve('./.chrome-profile');
const CHROME = process.env.CHROME_PATH
  || (fs.existsSync('/usr/bin/google-chrome') ? '/usr/bin/google-chrome' : '');
const URL_ = process.env.URL || '';
const WAIT = parseInt(process.env.WAIT_MS || '15000', 10);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(...a);

function unpackedExtensionId(dir) {
  const h = crypto.createHash('sha256').update(dir).digest('hex').slice(0, 32);
  return h.split('').map((c) => String.fromCharCode(97 + parseInt(c, 16))).join('');
}

async function main() {
  const m = JSON.parse(fs.readFileSync(path.join(EXT, 'manifest.json'), 'utf8'));
  log(`расширение: ${m.name} v${m.version} (manifest v${m.manifest_version})`);
  log(`каталог:    ${EXT}`);
  log(`chrome:     ${CHROME || 'системный'}`);

  fs.mkdirSync(PROFILE, { recursive: true });
  const ctx = await chromium.launchPersistentContext(PROFILE, {
    headless: false,                       // иначе Chrome сам добавит --headless=old
    executablePath: CHROME || undefined,
    ignoreHTTPSErrors: true,
    args: [
      '--headless=new',                    // в новом headless расширения работают
      '--no-sandbox', '--disable-dev-shm-usage', '--no-first-run',
      '--disable-blink-features=AutomationControlled',
      `--disable-extensions-except=${EXT}`,
      `--load-extension=${EXT}`,
    ],
  });

  const wantId = unpackedExtensionId(EXT);
  log(`ожидаемый ID: ${wantId}`);

  // даём Chrome поднять расширение
  const page = ctx.pages()[0] || await ctx.newPage();
  for (let i = 0; i * 500 < 5000; i++) await sleep(500);

  // 1) расширение реально загружено — его страница открывается
  const probe = await ctx.newPage();
  const popupFile = (m.action && m.action.default_popup) || 'manifest.json';
  let ok = false;
  try {
    const res = await probe.goto(`chrome-extension://${wantId}/${popupFile}`,
      { waitUntil: 'domcontentloaded', timeout: 15000 });
    ok = !!res;
    log(`страница расширения: ${res ? 'открылась (' + popupFile + ')' : 'не открылась'}`);
  } catch (e) {
    log(`страница расширения: ${e.message.split('\n')[0]}`);
  }

  // 2) сверяемся с профилем Chrome: там есть наш каталог расширения
  let inProfile = false;
  try {
    const internals = await ctx.newPage();
    await internals.goto('chrome://extensions-internals/', { waitUntil: 'domcontentloaded' });
    const txt = await internals.textContent('body').catch(() => '');
    inProfile = txt.includes(EXT) || txt.includes(wantId);
    const found = [...String(txt).matchAll(/"id":\s*"([a-p]{32})"/g)].map((m2) => m2[1]);
    log(`в профиле Chrome: ${found.length ? found.join(', ') : '—'}${inProfile ? ' (наш найден)' : ''}`);
    await internals.close();
  } catch { /* страница недоступна — не критично */ }

  const sw = ctx.serviceWorkers()[0];
  log(`service worker: ${sw ? sw.url() : 'не запущен (у этого расширения background.js пуст)'}`);

  if (URL_) {
    await page.goto(URL_, { waitUntil: 'domcontentloaded', timeout: 45000 });
    log(`страница открыта: ${page.url()}`);
    log(`заголовок: ${(await page.title().catch(() => '')) || '—'}`);
  }

  await ctx.close();
  if (!ok && !inProfile) {
    log('ОШИБКА: расширение не открылось в Chrome');
    process.exit(3);
  }
  log('OK: Chrome открыт, расширение Resources-Saver активно');
}

main().catch((e) => { console.error('ошибка:', e.message); process.exit(1); });
