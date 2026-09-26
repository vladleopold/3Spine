#!/usr/bin/env node
// Универсальный headless-ресурс-сейвер для CI (аналог Resources-Saver).
// Ловит ВСЮ сеть страницы (XHR/fetch/wasm/шрифты/изображения/JSON), раскрывает
// манифесты игр, умеет сам находить игровой шелл через API площадки и ходит
// по всей цепочке в одной развёрнутой сессии.
import { chromium } from 'playwright';
import fs from 'fs-extra';
import path from 'path';
import { createWriteStream } from 'fs';
import archiver from 'archiver';
import { URL } from 'url';
import fsSync from 'fs';

// системный Chrome, если не заданы playwright-браузеры (экономит ~150 МБ в CI)
function systemChrome() {
  const cands = [
    process.env.CHROME_PATH || '',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
  ];
  for (const c of cands) { if (c && fsSync.existsSync(c)) return c; }
  return '';
}
const EXEC = systemChrome();

const DEFAULT_URLS = [
  'https://first.ua/ua/igrovie-avtomaty/kendoo/4-gold-carts',
  'https://slotcity.ua/?modals=game&game-term=spinjoy-meduzas-fortune&demo=true',
  'https://cosmolot.ua/ua/game/demonic-dolls',
  'https://playson.com/game/clover-strike-hold-and-win',
  'https://3oaks.com/game/3_superpower_diamonds',
  'https://slotor777.ua/ru/game/view/56dab5f9954f459f919d800306e48b35?mode=demo',
  'https://beton.ua/game/pragmaticplay-direct-the-dog-house-megaways-1000?isDemo=true',
];

const URLS = (process.env.URLS || '')
  .split(',')
  .map((u) => u.trim())
  .filter(Boolean)
  .concat(process.env.URLS ? [] : DEFAULT_URLS);

const OUTPUT_DIR = process.env.OUTPUT_DIR || './downloaded';
const WAIT_AFTER_LOAD = parseInt(process.env.WAIT_MS || '35000', 10);
const HEADLESS = process.env.HEADLESS !== 'false';
const PROFILE = process.env.PROFILE || '';          // развёрнутая сессия
const PROXY = process.env.PROXY || '';              // http://user:pass@host:port
const GAME_URL = process.env.GAME_URL || '';        // уже найденный игровой шелл
const MANIFEST_DEPTH = parseInt(process.env.MANIFEST_DEPTH || '2', 10);
const SPINE_EXT = /\.(atlas|skel|bin)$/i;

const manifestUrls = new Set();                     // url -> из манифестов
const spineUrls = new Set();                        // найденные Spine-ассеты
const seenHosts = new Set();                        // все хосты, что реально стучались

const log = (...a) => console.log(...a);

// ---------------------------------------------------------------- пути
function sanitizePath(urlStr, baseDir) {
  try {
    const u = new URL(urlStr);
    let p = decodeURIComponent(u.pathname || '/');
    if (p.endsWith('/')) p += 'index.html';
    p = p.replace(/[<>:"|?*\x00-\x1f]/g, '_');
    if (u.search) {
      const hash = Buffer.from(u.search).toString('base64url').slice(0, 10);
      const ext = path.extname(p);
      const name = path.basename(p, ext) || 'file';
      p = path.join(path.dirname(p), `${name}-${hash}${ext}`);
    }
    return path.join(baseDir, u.hostname, p.replace(/^\//, ''));
  } catch {
    return path.join(baseDir, '_unknown', Date.now() + '.bin');
  }
}

// ---------------------------------------------------------------- манифесты
// Р��збирает JS/JSON-манифесты игр: пары "uuid": "images/foo.png",
// "path": ["res/x", 1], "files": [...] и т.п. — это то, чем славятся
// провайдеры (PlaysOnSite/Cocos, Kendoo и др.).
function harvest(src, base, depth, out, seen) {
  if (depth < 0 || !src || out.size > 6000) return;
  const s = src;
  const re = /"([0-9a-fA-F-]{8,})"\s*:\s*"([^"\\]{2,220})"/g;
  let m;
  while ((m = re.exec(s))) {
    if (!/\.(png|jpg|jpeg|webp|avif|atlas|skel|bin|json|mp3|wav|ogg|fnt|ttf|otf|woff2?|wasm|plist)$/i.test(m[2])) continue;
    let u = m[2];
    if (!/^https?:|^data:/.test(u)) {
      try { u = new URL(u, base).href; } catch { continue; }
    }
    if (!out.has(u) && !seen.has(u)) { out.add(u); seen.add(u); }
  }
  const re2 = /"(?:path|paths|file|files|assets|res|resources)"\s*:\s*\[([^\]]{0,8000})\]/g;
  while ((m = re2.exec(s))) {
    for (const q of m[1].matchAll(/"([^"\\]{2,220})"/g)) {
      const ext = path.extname(q[1]).toLowerCase();
      if (!['.png', '.jpg', '.jpeg', '.webp', '.avif', '.atlas', '.skel', '.bin',
        '.json', '.mp3', '.wav', '.ogg', '.fnt', '.ttf', '.otf', '.woff', '.woff2', '.wasm', '.plist'].includes(ext)) continue;
      let u = q[1];
      try { u = new URL(u, base).href; } catch { continue; }
      if (!out.has(u) && !seen.has(u)) { out.add(u); seen.add(u); }
    }
  }
}


// ---------------------------------------------------------------- резолвер шелла
// Страница казино — обёртка: сама игра лежит на отдельном хосте. Ищем её через
// API площадки (каталог игр → demo-endpoint) и идём туда в этой же сессии.
const SLUG_RE = /(?:game-term|game=|term=|\/game\/view\/|\/)([a-z0-9][a-z0-9_-]{3,60})\/?(?:$|[?#&])/i;

async function resolveGameUrl(ctx, page, pageUrl) {
  const slug = (() => {
    try {
      const u = new URL(pageUrl);
      const m = u.searchParams.get('game-term') || u.searchParams.get('game')
        || u.pathname.split('/').filter(Boolean).pop();
      return (m || SLUG_RE.exec(pageUrl)?.[1] || '').toLowerCase();
    } catch { return ''; }
  })();

  // 1) кандидаты в API-хосты: из уже пойманной сети + из HTML
  const hosts = new Set();
  for (const u of seenHosts) {
    const h = (() => { try { return new URL(u).hostname; } catch { return ''; } })();
    if (/^(api\d?v?\d*|api-gw|games-api)\./.test(h)) hosts.add(h);
  }
  try {
    const html = await page.content().catch(() => '');
    for (const m of html.matchAll(/"(https?:\/\/[a-z0-9.-]+)"/gi)) {
      try {
        const h = new URL(m[1].replace(/&quot;/g, '')).hostname;
        if (/^(api\d?v?\d*)\./.test(h)) hosts.add(h);
      } catch { /* не URL */ }
    }
  } catch { /* нет DOM */ }

  if (!hosts.size) return '';
  log(`  → ищу игру по «${slug || '?'}» через API: ${[...hosts].join(', ')}`);

  // 2) каталог игр площадки → provider/term/id
  for (const host of hosts) {
    const api = `https://${host}`;
    for (const cat of ['/games/providers/games', '/api/games/providers/games', '/games']) {
      let games;
      try {
        const r = await page.request.get(api + cat, { timeout: 12000 });
        if (!r.ok()) continue;
        games = await r.json();
      } catch { continue; }
      const flat = (Array.isArray(games) ? games : games?.games
        || games?.data?.games || games?.data || []).flat(9)
        .filter((g) => g && typeof g === 'object');
      const hit = flat.find((g) => {
        const t = String(g.term || g.slug || g.name_slug || '').toLowerCase();
        return slug && t && (t === slug || t.includes(slug) || slug.includes(t));
      });
      if (!hit) continue;
      const provider = hit.provider || hit.provider_name || hit.p || '';
      const term = hit.term || hit.slug || slug;
      const id = hit.id || hit.game_id || '';
      log(`  → каталог: игра «${hit.name || hit.title || term}» (id=${id}, провайдер=${provider})`);

      // 3) demo-endpoint → реальный игровой URL
      for (const u of [
        `${api}/games/demo?provider=${encodeURIComponent(provider)}&term=${encodeURIComponent(term)}`,
        `${api}/games/${id}/demo`,
        `${api}/games/play?provider=${encodeURIComponent(provider)}&term=${encodeURIComponent(term)}&demo=true`,
      ]) {
        try {
          const r = await page.request.get(u, { timeout: 12000 });
          if (!r.ok()) continue;
          const j = await r.json();
          const g = typeof j === 'string' ? j : (j.url || j.game_url || j.launch_url
            || j.data?.url || j.game?.url);
          if (g && /^https?:/i.test(g)) {
            log(`  → запускающий URL: ${g.slice(0, 120)}`);
            return g;
          }
        } catch { /* следующий вариант */ }
      }
    }
  }
  return '';
}

// ---------------------------------------------------------------- сохранение
async function saveBytes(resUrl, buf, outRoot, saved) {
  if (!buf || buf.length === 0) return false;
  if (/^data:|^blob:|^chrome-extension:/.test(resUrl)) return false;
  if (saved.has(resUrl)) return false;
  const localPath = sanitizePath(resUrl, outRoot);
  await fs.ensureDir(path.dirname(localPath));
  await fs.writeFile(localPath, buf);
  saved.set(resUrl, localPath);
  try { seenHosts.add(new URL(resUrl).hostname); } catch { /* не URL */ }
  const kb = (buf.length / 1024).toFixed(1);
  const tag = SPINE_EXT.test(resUrl) ? '★SPINE' : '     ';
  log(`  ✓ ${tag} ${kb.padStart(8)} KB  ${resUrl.slice(0, 110)}`);
  if (SPINE_EXT.test(resUrl)) spineUrls.add(resUrl);
  return true;
}

async function saveResponse(response, outRoot, saved) {
  try {
    const resUrl = response.url();
    if (!resUrl) return;
    const status = response.status();
    if (status < 200 || status >= 400) return;
    let body = null;
    try { body = await response.body(); } catch { return; }
    if (!body || body.length === 0) return;
    await saveBytes(resUrl, body, outRoot, saved);
    // манифесты разбираем сразу
    if (/\.(js|json)(\?|$)/i.test(resUrl) && body.length < 4_000_000) {
      harvest(body.toString('utf8'), new URL(resUrl).href, MANIFEST_DEPTH, manifestUrls, saved);
    }
  } catch { /* отдельные ответы игнорируем */ }
}

// ---------------------------------------------------------------- один URL
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36';

async function openCtx() {
  const opts = {
    headless: HEADLESS,
    ...(EXEC ? { executablePath: EXEC } : {}),
    args: [
      '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
      '--disable-gpu', '--disable-web-security',
      '--disable-features=IsolateOrigins,site-per-process',
      '--allow-running-insecure-content', '--autoplay-policy=no-user-gesture-required',
      '--disable-blink-features=AutomationControlled',
    ],
  };
  if (PROFILE) {
    await fs.ensureDir(PROFILE);
    return await chromium.launchPersistentContext(PROFILE, {
      ...opts,
      proxy: PROXY ? { server: PROXY } : undefined,
      ignoreHTTPSErrors: true,
      viewport: { width: 1920, height: 1080 },
      locale: 'uk-UA',
      extraHTTPHeaders: { 'Accept-Language': 'uk-UA,uk;q=0.9,en;q=0.8' },
      userAgent: UA,
    });
  }
  const b = await chromium.launch(opts);
  const c = await b.newContext({
    proxy: PROXY ? { server: PROXY } : undefined,
    viewport: { width: 1920, height: 1080 },
    ignoreHTTPSErrors: true,
    javaScriptEnabled: true,
    locale: 'uk-UA',
    extraHTTPHeaders: { 'Accept-Language': 'uk-UA,uk;q=0.9,en;q=0.8' },
    permissions: ['clipboard-read', 'clipboard-write'],
    userAgent: UA,
  });
  c.__browser = b;
  return c;
}

async function closeCtx(ctx) {
  try { await (ctx.__browser ? ctx.__browser.close() : ctx.close()); } catch { /* уже закрыт */ }
}

async function capturePage(ctx, url, outRoot, saved) {
  log(`\n════ ${url}`);
  const page = ctx.pages()[0] || await ctx.newPage();
  // снимаем признак автоматизации (Cloudflare / fingerprint-чеки)
  await ctx.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  }).catch(() => {});
  page.on('response', (r) => saveResponse(r, outRoot, saved));

  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
    log('  → страница открыта');
  } catch (e) {
    log(`  ! goto: ${e.message.split('\n')[0]}`);
  }

  // проход по двум барьерам: age-gate (18+) и кнопка запуска игры
  const STEPS = [
    ['age-gate', ['text=Yes', 'text=Так', 'text=I am 18', 'text=Мне есть 18',
      'text=Enter', 'text=Войти', 'button:has-text("18")', 'button:has-text("Yes")',
      'button:has-text("Так")', '[class*="age"] button', '[id*="age"] button']],
    ['play/demo', ['text=Play', 'text=Demo', 'text=Играть', 'text=Демо', 'text=Start',
      'text=PLAY DEMO', 'text=Play Demo', 'text=Грати', 'text=Запустить',
      'text=Открыть игру', 'button:has-text("Play")', 'button:has-text("Demo")',
      'button:has-text("Играть")', 'button:has-text("Демо")', 'a[href*="demo"]',
      '[class*="play"]', '[class*="demo"]', '[id*="play"]', '[id*="demo"]',
      'canvas', 'body']],
  ];
  for (const [name, sels] of STEPS) {
    let done = false;
    for (const sel of sels) {
      if (done) break;
      try {
        const el = page.locator(sel).first();
        if (!(await el.isVisible({ timeout: 1200 }))) continue;
        const box = await el.boundingBox().catch(() => null);
        if (box) {                      // настоящий клик мышью по координатам
          await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
        } else {
          await el.click({ timeout: 2000 });
        }
        log(`  → клик [${name}]: ${sel}`);
        await page.waitForTimeout(3500);
        done = true;
      } catch { /* нет такого — идём дальше */ }
    }
  }
  // игра живёт в iframe — дожидаемся появления и шевелим мышью/скроллим
  try {
    await page.waitForSelector('iframe', { timeout: 10000 });
    log('  → iframe с игрой обнаружен');
  } catch { }
  await page.mouse.move(600, 400).catch(() => {});
  await page.mouse.move(900, 600).catch(() => {});
  await page.evaluate(() => window.scrollBy(0, 300)).catch(() => {});

  // догрузка: короткими циклами, без долгого ожидания
  const deadline = Date.now() + WAIT_AFTER_LOAD;
  while (Date.now() < deadline) {
    try { await page.waitForLoadState('networkidle', { timeout: 4000 }); }
    catch { try { await page.waitForTimeout(2000); } catch { break; } }
  }

  // CDP-финальный сбор — так же, как Resources-Saver через DevTools
  try {
    const client = await ctx.newCDPSession(page);
    await client.send('Network.enable');
    await client.send('Page.enable');
    const { frameTree } = await client.send('Page.getResourceTree');
    const walk = async (fr) => {
      for (const r of fr.resources || []) {
        if (!r.url || saved.has(r.url)) continue;
        try {
          const { content, base64Encoded } = await client.send('Page.getResourceContent', {
            frameId: fr.frame.id, url: r.url,
          });
          if (content) {
            const buf = base64Encoded ? Buffer.from(content, 'base64') : Buffer.from(content, 'utf8');
            await saveBytes(r.url, buf, outRoot, saved);
          }
        } catch { /* evicted из кеша */ }
      }
      for (const ch of fr.childFrames || []) await walk(ch);
    };
    await walk(frameTree);
  } catch { /* не критично */ }

  return page;
}

function countSpine(dir) {
  let atlas = 0, skel = 0;
  const walk = (d) => {
    let items = [];
    try { items = fsSync.readdirSync(d, { withFileTypes: true }); } catch { return; }
    for (const it of items) {
      const p = path.join(d, it.name);
      if (it.isDirectory()) walk(p);
      else if (/\.atlas$/i.test(it.name)) atlas++;
      else if (/\.(skel|bin)$/i.test(it.name)) skel++;
    }
  };
  walk(dir);
  return Math.min(atlas, skel);
}

// ---------------------------------------------------------------- ZIP
function createZip(srcDir, zipPath) {
  return new Promise((resolve, reject) => {
    const output = createWriteStream(zipPath);
    const archive = archiver('zip', { zlib: { level: 6 } });
    output.on('close', resolve);
    archive.on('error', reject);
    archive.pipe(output);
    archive.directory(srcDir, false);
    archive.finalize();
  });
}

// ---------------------------------------------------------------- main
async function main() {
  if (!URLS.length) {
    log('Нет URL. Передай URLS="url1,url2,..."');
    process.exit(1);
  }
  await fs.emptyDir(OUTPUT_DIR);
  const all = new Map();

  for (const url of URLS) {
    const safe = url.replace(/^https?:\/\//, '').replace(/[^a-zA-Z0-9._-]/g, '_').slice(0, 70);
    const dir = path.join(OUTPUT_DIR, safe);
    await fs.ensureDir(dir);

    // развёрнутая сессия: один контекст на все проходы — куки/память игры живут
    const ctx = await openCtx();
    let page = await capturePage(ctx, url, dir, all);
    const spineBefore = spineUrls.size;

    // проход 2: если Spine не нашлись — сами находим игровой шелл через API
    if (!spineBefore) {
      const game = GAME_URL && GAME_URL !== url ? GAME_URL : await resolveGameUrl(ctx, page, url);
      if (game) {
        log('  → Spine на странице нет, иду в игровой шелл (та же сессия)');
        page = await capturePage(ctx, game, dir, all);
        // проход 3: шелл мог подставить ещё одну ссылку
        if (!spineUrls.size) {
          const g2 = await resolveGameUrl(ctx, page, game);
          if (g2 && g2 !== game) await capturePage(ctx, g2, dir, all);
        }
      }
    }
    await closeCtx(ctx);

    // добираем файлы из манифестов (то, что страница не запросила, но нужно)
    if (manifestUrls.size) {
      log(`  → добираю из манифестов: ${manifestUrls.size}`);
      const ctx2 = await chromium.launchPersistentContext(PROFILE || path.join(OUTPUT_DIR, '.tmp-profile'),
        { headless: HEADLESS, ...(EXEC ? { executablePath: EXEC } : {}),
          args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-web-security'] });
      const p2 = ctx2.pages()[0] || await ctx2.newPage();
      let n = 0;
      for (const u of Array.from(manifestUrls).slice(0, 4000)) {
        try {
          const r = await p2.request.get(u, { timeout: 12000 });
          if (r.ok()) { const b = await r.body(); if (await saveBytes(u, b, dir, all)) n++; }
        } catch { }
        if (n > 1200) break;
      }
      await ctx2.close().catch(() => {});
      log(`  → добрано ${n}`);
    }

    const cnt = all.size;
    const spine = all.size ? countSpine(dir) : 0;
    log(`\n  Итого по ${safe}: ${cnt} файлов (Spine: ${spine})`);
    if (cnt > 0) {
      const zip = path.join(OUTPUT_DIR, `${safe}.zip`);
      await createZip(dir, zip);
      log(`  → ZIP: ${zip}`);
    }
    await fs.writeJson(path.join(dir, 'saver-report.json'), {
      url, files: cnt, spine, manifestUrls: manifestUrls.size,
    }, { spaces: 2 });
  }
  log('\nГотово.');
}

// тесты импортируют модуль — main() только при прямом запуске
const isDirectRun = process.argv[1] && process.argv[1].endsWith('download-resources.mjs');
if (isDirectRun) {
  main().catch((e) => { console.error(e); process.exit(1); });
}

export { sanitizePath, harvest, countSpine, resolveGameUrl, openCtx, closeCtx, capturePage };
