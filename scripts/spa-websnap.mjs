#!/usr/bin/env node
// Обход состояний SPA с перехватом всех ответов браузера.
// Аналог uirip/websnap: у BFS по дереву состояний, дедуп по SHA-256,
// перехват сетевых ответов, офлайн-HTML со ссылками на _assets.
import fs from 'fs';
import fsp from 'fs/promises';
import path from 'path';
import crypto from 'crypto';
import { chromium } from 'playwright';

const URL_ = process.env.URL || '';
const OUT = path.resolve(process.env.OUTPUT_DIR || './artifacts');
const DEPTH = parseInt(process.env.DEPTH || '3', 10);
const MAX_STATES = parseInt(process.env.MAX_STATES || '40', 10);
const PER_STATE = parseInt(process.env.PER_STATE || '10', 10);
const PER_TEMPLATE = parseInt(process.env.MAX_PER_TEMPLATE || '2', 10);
const SETTLE = parseInt(process.env.WAIT_MS || '1200', 10);
const VIEWPORT = process.env.VIEWPORT || '1440x900';
const INCLUDE = (process.env.INCLUDE || '').trim();
const EXCLUDE = (process.env.EXCLUDE || '').trim();

const SYSTEM_CHROME = [
  process.env.CHROME_PATH,
  '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
].filter(Boolean).find((p) => { try { return fs.existsSync(p); } catch { return false; } });

const ROLES = new Set(['button', 'link', 'tab', 'checkbox', 'radio', 'menuitem',
  'menuitemcheckbox', 'menuitemradio', 'option', 'combobox', 'switch', 'treeitem', 'tabpanel']);

const t0 = Date.now();
const log = (...a) => console.log(...a);
const since = () => `${((Date.now() - t0) / 1000).toFixed(1)}с`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const sha = (s) => crypto.createHash('sha256').update(s).digest('hex');

const assets = new Map();      // url -> relative file
const domains = new Set();
let bytes = 0;

function safeName(s) {
  return String(s).trim().replace(/[^\p{L}\p{N}._-]+/gu, '-').replace(/^-+|-+$/g, '').slice(0, 60) || 'state';
}

async function saveAsset(url, body) {
  if (assets.has(url)) return;
  let u;
  try { u = new URL(url); } catch { return; }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return;
  const dom = u.hostname.replace(/[^a-z0-9]+/gi, '_');
  let p;
  try { p = decodeURIComponent(u.pathname); } catch { p = u.pathname; }
  if (!p || p === '/') p = '/index.html';
  if (p.endsWith('/')) p += 'index.html';
  if (!/\.[a-z0-9]{1,8}$/i.test(p)) p += path.extname(new URL(url).search ? '.bin' : '') || '.bin';
  const rel = path.join('_assets', dom, p.replace(/^\/+/, ''));
  const file = path.join(OUT, rel);
  await fsp.mkdir(path.dirname(file), { recursive: true });
  await fsp.writeFile(file, body);
  assets.set(url, rel);
  domains.add(dom);
  bytes += body.length;
}

function rewrite(html) {
  let out = html;
  for (const [url, rel] of assets) {
    if (!out.includes(url)) continue;
    out = out.split(url).join(rel.split(path.sep).join('/'));
  }
  return out;
}

function collectActions(node, acc = []) {
  if (!node) return acc;
  if (Array.isArray(node)) { for (const n of node) collectActions(n, acc); return acc; }
  if (ROLES.has(node.role) && node.name) acc.push({ role: node.role, name: String(node.name) });
  for (const key of ['children']) collectActions(node[key], acc);
  return acc;
}

async function settle(page) {
  await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
  await sleep(SETTLE);
}

async function applyActions(page, actions) {
  for (const a of actions) {
    const loc = page.getByRole(a.role, { name: a.name, exact: false }).first();
    if (await loc.count().catch(() => 0)) {
      await loc.click({ timeout: 4000 }).catch(() => {});
      await settle(page);
    }
  }
}

async function main() {
  if (!URL_) throw new Error('не задан URL');
  await fsp.mkdir(OUT, { recursive: true });

  const [vw, vh] = VIEWPORT.split('x').map((n) => parseInt(n, 10) || 800);
  const opts = {
    headless: true,
    viewport: { width: vw, height: vh },
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-blink-features=AutomationControlled'],
  };
  if (SYSTEM_CHROME) opts.executablePath = SYSTEM_CHROME;
  log(`браузер: ${SYSTEM_CHROME || 'playwright chromium'}`);
  log(`глубина ${DEPTH} | состояний максимум ${MAX_STATES} | на состояние ${PER_STATE} | пауз ${SETTLE} мс`);

  const browser = await chromium.launch(opts);
  const ctx = await browser.newContext({ viewport: { width: vw, height: vh } });
  const page = await ctx.newPage();

  page.on('response', async (res) => {
    const type = res.request().resourceType();
    if (type === 'document' || type === 'other' || type === 'eventsource') return;
    try { await saveAsset(res.url(), await res.body()); } catch { /* ответ без тела */ }
  });

  const templates = new Map();
  const seen = new Set();
  const tree = [];
  const queue = [{ label: 'homepage', actions: [], depth: 0 }];

  while (queue.length && tree.length < MAX_STATES && since() !== null) {
    const state = queue.shift();
    await page.goto(URL_, { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {});
    await settle(page);
    await applyActions(page, state.actions);

    let html = await page.content().catch(() => '');
    const hash = sha(html);
    if (seen.has(hash)) { log(`  ↺ повтор состояния: ${state.label}`); continue; }
    seen.add(hash);

    const tpl = sha(html.replace(/[0-9a-f]{6,}|\d+/gi, '#'));
    const tplCount = templates.get(tpl) || 0;
    if (tplCount >= PER_TEMPLATE) { log(`  ⤳ шаблон исчерпан: ${state.label}`); continue; }
    templates.set(tpl, tplCount + 1);

    const label = state.actions.length ? `${safeName(state.label)}--${safeName(state.actions[state.actions.length - 1].name)}` : 'homepage';
    const file = `${safeName(label)}.html`;
    await fsp.writeFile(path.join(OUT, file), rewrite(html));
    tree.push({ label, file, depth: state.depth, actions: state.actions, sha: hash.slice(0, 12) });
    log(`  ✓ ${file} (состояний ${tree.length}, ассетов ${assets.size}, ${since()})`);
    if (state.depth >= DEPTH) continue;

    let candidates = collectActions(await page.accessibility.snapshot({ interestingOnly: true }).catch(() => null));
    const seenAct = new Set();
    candidates = candidates.filter((a) => {
      const key = `${a.role}|${a.name}`;
      if (seenAct.has(key)) return false;
      seenAct.add(key);
      return true;
    });
    if (INCLUDE) {
      const kept = [];
      for (const a of candidates) {
        const loc = page.getByRole(a.role, { name: a.name, exact: false }).first();
        if (await loc.count().catch(() => 0)) {
          for (const sel of INCLUDE.split(',').map((s) => s.trim()).filter(Boolean)) {
            if (await loc.locator(sel).count().catch(() => 0)) { kept.push(a); break; }
            if (await page.locator(`${sel}:has-text("${a.name.replace(/"/g, '')}")`).count().catch(() => 0)) { kept.push(a); break; }
          }
        }
      }
      candidates = kept;
    }
    if (EXCLUDE) {
      const kept = [];
      for (const a of candidates) {
        const loc = page.getByRole(a.role, { name: a.name, exact: false }).first();
        let skip = false;
        for (const sel of EXCLUDE.split(',').map((s) => s.trim()).filter(Boolean)) {
          if (await loc.locator(sel).count().catch(() => 0)) { skip = true; break; }
          if (await page.locator(`${sel}:has-text("${a.name.replace(/"/g, '')}")`).count().catch(() => 0)) { skip = true; break; }
        }
        if (!skip) kept.push(a);
      }
      candidates = kept;
    }
    log(`    найдено интерактивных: ${candidates.length}`);
    for (const a of candidates.slice(0, PER_STATE)) {
      if (tree.length + queue.length >= MAX_STATES) break;
      queue.push({ label: state.label, actions: [...state.actions, a], depth: state.depth + 1 });
    }
  }

  await fsp.writeFile(path.join(OUT, 'bundle.json'), JSON.stringify({
    url: URL_, depth: DEPTH, generated: new Date().toISOString(),
    assets: assets.size, domains: [...domains], bytes, states: tree,
  }, null, 2));

  await browser.close();

  const count = (re) => {
    let n = 0;
    const walk = (d) => { for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p); else if (re.test(e.name)) n++;
    } };
    try { walk(OUT); } catch { /* каталога нет */ }
    return n;
  };
  log(`готово за ${since()}: состояний ${tree.length}, ассетов ${assets.size}, доменов ${domains.size}, байт ${bytes}`);
  log(`Spine: atlas=${count(/\.atlas$/i)} skel=${count(/\.(skel|json)$/i)} bin=${count(/\.bin$/i)} картинок=${count(/\.(png|jpe?g|webp|avif|gif)$/i)}`);
  if (assets.size === 0) throw new Error('ни одного ассета не перехвачено');
}

main()
  .then(() => process.exit(0))
  .catch((e) => { console.error('ошибка обхода SPA:', e.message); process.exit(1); });
