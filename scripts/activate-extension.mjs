#!/usr/bin/env node
// Активирует расширение в Chrome: запускаем системный Chrome с --load-extension
// и убеждаемся, что расширение реально загрузилось и зарегистрировало свой
// background/service worker. Ничего не качаем — только Chrome из образа runner'а.
import { chromium } from 'playwright';
import fs from 'fs';
import path from 'path';

const EXT = path.resolve(process.env.EXT_DIR || './.chrome-ext');
const PROFILE = process.env.PROFILE || path.join(EXT, '..', '.chrome-profile');
const CHROME = process.env.CHROME_PATH
  || (fs.existsSync('/usr/bin/google-chrome') ? '/usr/bin/google-chrome' : '');
const WAIT = parseInt(process.env.WAIT_MS || '15000', 10);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(...a);

async function main() {
  const manifest = JSON.parse(fs.readFileSync(path.join(EXT, 'manifest.json'), 'utf8'));
  log(`расширение: ${manifest.name} v${manifest.version} (manifest v${manifest.manifest_version})`);
  log(`chrome: ${CHROME || 'системный по умолчанию'}`);

  fs.mkdirSync(PROFILE, { recursive: true });
  const ctx = await chromium.launchPersistentContext(PROFILE, {
    headless: false,   // расширения не работают в --headless=old
    executablePath: CHROME || undefined,
    args: [
      '--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--no-first-run',
      '--disable-features=DialMediaRouteProvider,OptimizationHints',
      `--disable-extensions-except=${EXT}`,
      `--load-extension=${EXT}`,
    ],
  });

  // 1) service worker расширения (MV3) либо background page (MV2)
  let url = '';
  for (let i = 0; i * 500 < WAIT; i++) {
    const sw = ctx.serviceWorkers()[0];
    const bg = ctx.backgroundPages()[0];
    if (sw) { url = sw.url(); break; }
    if (bg) { url = bg.url(); break; }
    await sleep(500);
  }
  if (!url) {
    await ctx.close();
    log('ОШИБКА: расширение не активировалось (нет service worker)');
    process.exit(3);
  }
  const id = new URL(url).host;
  log(`активировано: chrome-extension://${id}/`);

  // 2) страница расширения должна открываться (значит разрешения выданы)
  const page = await ctx.newPage();
  const res = await page.goto(`chrome-extension://${id}/${manifest.action?.default_popup
    || manifest.devtools_page || 'manifest.json'}`, { waitUntil: 'domcontentloaded' })
    .catch((e) => ({ err: e.message }));
  if (res && res.err) log(`страница расширения: ${res.err.split('\n')[0]}`);
  else log('страница расширения открылась');

  await ctx.close();
  log('OK: расширение установлено и активно в Chrome');
}

main().catch((e) => { console.error(e.message); process.exit(1); });
