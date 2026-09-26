#!/usr/bin/env node
// Обход состояний SPA с перехватом всех ответов браузера.
// CLI как у uirip/websnap: open / auto / tree / done / snap / click / goto /
// fill / status / kill. Флаги: --output --viewport --wait --depth --max-states
// --include --exclude --no-headless --session --port. Переменные окружения
// работают как значения по умолчанию.
import fs from 'fs';
import fsp from 'fs/promises';
import path from 'path';
import net from 'net';
import os from 'os';
import crypto from 'crypto';
import { spawn } from 'child_process';
import { chromium } from 'playwright';

const SELF = path.resolve(process.argv[1]);
let keepAlive = false;      // демон не должен завершаться после listen

const ROLES = new Set(['button', 'link', 'tab', 'checkbox', 'radio', 'menuitem',
  'menuitemcheckbox', 'menuitemradio', 'option', 'combobox', 'switch', 'treeitem', 'tabpanel']);

const t0 = Date.now();
const log = (...a) => console.log(...a);
const since = () => `${((Date.now() - t0) / 1000).toFixed(1)}с`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const sha = (s) => crypto.createHash('sha256').update(s).digest('hex');
const safeName = (s) => String(s).trim().replace(/[^\p{L}\p{N}._-]+/gu, '-')
  .replace(/^-+|-+$/g, '').slice(0, 60) || 'state';

const SESSION_DIR = path.join(process.env.SESSION_DIR || path.join(os.homedir(), '.spa-websnap'), '');
const sessionFile = (name) => path.join(SESSION_DIR, `${name}.json`);

function parseArgs(argv) {
  const flags = {};
  const positional = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--') { positional.push(...argv.slice(i + 1)); break; }
    if (!a.startsWith('-')) { positional.push(a); continue; }
    const key = a.replace(/^--?/, '');
    if (key.startsWith('no-')) { flags[camel(key.slice(3))] = false; continue; }
    const next = argv[i + 1];
    if (next === undefined || next.startsWith('-')) { flags[camel(key)] = true; continue; }
    flags[camel(key)] = next;
    i++;
  }
  return { flags, positional };
}
const camel = (s) => s.replace(/-([a-z])/g, (_, c) => c.toUpperCase());

function systemChrome() {
  return [process.env.CHROME_PATH, '/usr/bin/google-chrome', '/usr/bin/chromium',
    '/usr/bin/chromium-browser'].filter(Boolean)
    .find((p) => { try { return fs.existsSync(p); } catch { return false; } });
}

class Capture {
  constructor(opts) {
    this.url = opts.url;
    this.out = path.resolve(opts.output || './output');
    this.depth = parseInt(opts.depth ?? 3, 10);
    this.maxStates = parseInt(opts.maxStates ?? 40, 10);
    this.perState = parseInt(opts.perState ?? 10, 10);
    this.perTemplate = parseInt(opts.maxPerTemplate ?? 2, 10);
    this.wait = parseInt(opts.wait ?? 1200, 10);
    this.viewport = opts.viewport || '1440x900';
    this.include = (opts.include || '').trim();
    this.exclude = (opts.exclude || '').trim();
    this.headless = opts.headless !== false;
    this.assets = new Map();
    this.domains = new Set();
    this.bytes = 0;
    this.tree = [];
    this.templates = new Map();
    this.seen = new Set();
    this.browser = null;
    this.page = null;
  }

  async launch() {
    const [vw, vh] = this.viewport.split('x').map((n) => parseInt(n, 10) || 800);
    const opts = {
      headless: this.headless,
      viewport: { width: vw, height: vh },
      args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-blink-features=AutomationControlled'],
    };
    const exe = systemChrome();
    if (exe) opts.executablePath = exe;
    this.browser = await chromium.launch(opts);
    const ctx = await this.browser.newContext({ viewport: { width: vw, height: vh } });
    this.page = await ctx.newPage();
    this.page.on('response', async (res) => {
      const type = res.request().resourceType();
      if (type === 'document' || type === 'other' || type === 'eventsource') return;
      try { await this.saveAsset(res.url(), await res.body()); } catch { /* без тела */ }
    });
    log(`браузер: ${exe || 'playwright chromium'} | окно ${vw}x${vh} | ${this.headless ? 'headless' : 'видимый'}`);
  }

  async saveAsset(url, body) {
    if (this.assets.has(url)) return;
    let u;
    try { u = new URL(url); } catch { return; }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return;
    const dom = u.hostname.replace(/[^a-z0-9]+/gi, '_');
    let p;
    try { p = decodeURIComponent(u.pathname); } catch { p = u.pathname; }
    if (!p || p === '/') p = '/index.html';
    if (p.endsWith('/')) p += 'index.html';
    if (!/\.[a-z0-9]{1,8}$/i.test(p)) p += '.bin';
    const rel = path.join('_assets', dom, p.replace(/^\/+/, ''));
    const file = path.join(this.out, rel);
    await fsp.mkdir(path.dirname(file), { recursive: true });
    await fsp.writeFile(file, body);
    this.assets.set(url, rel);
    this.domains.add(dom);
    this.bytes += body.length;
  }

  rewrite(html) {
    let out = html;
    for (const [url, rel] of this.assets) {
      if (!out.includes(url)) continue;
      out = out.split(url).join(rel.split(path.sep).join('/'));
    }
    return out;
  }

  async settle() {
    await this.page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
    await sleep(this.wait);
  }

  async scan() {
    return await this.page.evaluate((roleNames) => {
      const roles = roleNames;
      const out = [];
      const nameOf = (el) => (el.getAttribute('aria-label') || el.getAttribute('title')
        || el.textContent || el.getAttribute('alt') || el.value || '').trim().replace(/\s+/g, ' ').slice(0, 60);
      const roleOf = (el) => {
        const explicit = (el.getAttribute('role') || '').toLowerCase();
        if (explicit && roles.includes(explicit)) return explicit;
        const tag = el.tagName.toLowerCase();
        if (tag === 'a') return el.hasAttribute('href') ? 'link' : null;
        if (tag === 'button' || tag === 'summary') return 'button';
        if (tag === 'select') return 'combobox';
        if (tag === 'input') {
          const t = (el.getAttribute('type') || 'text').toLowerCase();
          if (t === 'checkbox') return 'checkbox';
          if (t === 'radio') return 'radio';
          if (t === 'submit' || t === 'button' || t === 'reset') return 'button';
          return null;
        }
        return null;
      };
      const visible = (el) => {
        const r = el.getBoundingClientRect();
        if (r.width <= 2 || r.height <= 2) return false;
        const st = getComputedStyle(el);
        return st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
      };
      for (const el of document.querySelectorAll('a[href], button, summary, select, [role], [onclick], [tabindex]')) {
        const role = roleOf(el);
        if (!role || !roles.includes(role)) continue;
        if (!visible(el)) continue;
        const name = nameOf(el);
        if (!name) continue;
        if (role === 'link') {
          const href = el.getAttribute('href') || '';
          if (/^(https?:)?\/\//.test(href)) {
            try { if (new URL(href, location.href).origin !== location.origin) continue; } catch { continue; }
          }
        }
        out.push({ role, name, tag: el.tagName.toLowerCase() });
      }
      return out;
    }, [...ROLES]).catch(() => []);
  }

  async click(a) {
    const byRole = this.page.getByRole(a.role, { name: a.name, exact: false }).first();
    if (await byRole.count().catch(() => 0)) { await byRole.click({ timeout: 4000 }).catch(() => {}); return true; }
    const safe = String(a.name).replace(/"/g, '');
    const byTag = this.page.locator(`${a.tag || 'button'}:has-text("${safe}")`).first();
    if (await byTag.count().catch(() => 0)) { await byTag.click({ timeout: 4000 }).catch(() => {}); return true; }
    return false;
  }

  async filter(list) {
    let out = list;
    const seen = new Set();
    out = out.filter((a) => {
      const k = `${a.role}|${a.name}`;
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    });
    if (this.include) {
      const kept = [];
      for (const a of out) {
        const safe = a.name.replace(/"/g, '');
        for (const sel of this.include.split(',').map((s) => s.trim()).filter(Boolean)) {
          if (await this.page.locator(`${sel}:has-text("${safe}")`).count().catch(() => 0)) { kept.push(a); break; }
        }
      }
      out = kept;
    }
    if (this.exclude) {
      const kept = [];
      for (const a of out) {
        const safe = a.name.replace(/"/g, '');
        let skip = false;
        for (const sel of this.exclude.split(',').map((s) => s.trim()).filter(Boolean)) {
          if (await this.page.locator(`${sel}:has-text("${safe}")`).count().catch(() => 0)) { skip = true; break; }
        }
        if (!skip) kept.push(a);
      }
      out = kept;
    }
    return out;
  }

  async capture(label, actions, depth) {
    let html = await this.page.content().catch(() => '');
    const hash = sha(html);
    if (this.seen.has(hash)) { log(`  ↺ повтор состояния: ${label}`); return null; }
    this.seen.add(hash);
    const tpl = sha(html.replace(/[0-9a-f]{6,}|\d+/gi, '#'));
    const n = this.templates.get(tpl) || 0;
    if (n >= this.perTemplate) { log(`  ⤳ шаблон исчерпан: ${label}`); return null; }
    this.templates.set(tpl, n + 1);
    const file = `${safeName(label)}.html`;
    await fsp.writeFile(path.join(this.out, file), this.rewrite(html));
    this.tree.push({ label, file, depth, actions, sha: hash.slice(0, 12) });
    log(`  ✓ ${file} (состояний ${this.tree.length}, ассетов ${this.assets.size}, ${since()})`);
    return file;
  }

  async open() {
    await fsp.mkdir(this.out, { recursive: true });
    if (!this.page) await this.launch();
    await this.page.goto(this.url, { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {});
    await this.settle();
    return 'открыто';
  }

  async auto() {
    if (!this.page) throw new Error('сначала выполните open');
    log(`глубина ${this.depth} | состояний максимум ${this.maxStates} | на состояние ${this.perState} | пауз ${this.wait} мс`);
    const queue = [{ label: 'homepage', actions: [], depth: 0 }];
    while (queue.length && this.tree.length < this.maxStates) {
      const st = queue.shift();
      await this.page.goto(this.url, { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {});
      await this.settle();
      for (const a of st.actions) { await this.click(a); await this.settle(); }
      const file = await this.capture(st.label, st.actions, st.depth);
      if (!file) continue;
      if (st.depth >= this.depth) continue;
      const cands = await this.filter(await this.scan());
      log(`    найдено интерактивных: ${cands.length}`);
      for (const a of cands.slice(0, this.perState)) {
        if (this.tree.length + queue.length >= this.maxStates) break;
        const label = `${st.label}--${a.name}`;
        queue.push({ label, actions: [...st.actions, a], depth: st.depth + 1 });
      }
    }
    return this.summary();
  }

  async snap(label) {
    if (!this.page) throw new Error('сначала выполните open');
    const name = label || `snap-${this.tree.length + 1}`;
    const file = await this.capture(name, [], this.tree.length);
    return file || `состояние ${name} уже было`;
  }

  async clickCmd(role, name) {
    if (!this.page) throw new Error('сначала выполните open');
    const ok = await this.click({ role, name, tag: role });
    await this.settle();
    return ok ? `нажато: ${role} "${name}"` : `не найдено: ${role} "${name}"`;
  }

  async fillCmd(role, name, value) {
    if (!this.page) throw new Error('сначала выполните open');
    const loc = this.page.getByRole(role, { name, exact: false }).first();
    if (await loc.count().catch(() => 0)) { await loc.fill(value, { timeout: 4000 }).catch(() => {}); return `заполнено: ${name}`; }
    const alt = this.page.locator(`input[name="${name}"]`).first();
    if (await alt.count().catch(() => 0)) { await alt.fill(value, { timeout: 4000 }).catch(() => {}); return `заполнено: ${name}`; }
    return `не найдено поле: ${name}`;
  }

  async goto(url) {
    if (!this.page) throw new Error('сначала выполните open');
    this.url = url;
    await this.page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {});
    await this.settle();
    return `переход: ${url}`;
  }

  summary() {
    return `состояний ${this.tree.length}, ассетов ${this.assets.size}, доменов ${this.domains.size}, байт ${this.bytes}`;
  }

  counts() {
    const count = (re) => {
      let n = 0;
      const walk = (d) => {
        let entries = [];
        try { entries = fs.readdirSync(d, { withFileTypes: true }); } catch { return; }
        for (const e of entries) {
          const p = path.join(d, e.name);
          if (e.isDirectory()) walk(p);
          else if (re.test(e.name)) n++;
        }
      };
      walk(this.out);
      return n;
    };
    return `atlas=${count(/\.atlas$/i)} skel=${count(/\.(skel|json)$/i)} bin=${count(/\.bin$/i)} картинок=${count(/\.(png|jpe?g|webp|avif|gif)$/i)}`;
  }

  state() {
    return {
      url: this.url, output: this.out, depth: this.depth, maxStates: this.maxStates,
      wait: this.wait, viewport: this.viewport, headless: this.headless,
      include: this.include, exclude: this.exclude, perState: this.perState,
      maxPerTemplate: this.perTemplate, started: new Date().toISOString(),
      assets: this.assets.size, domains: [...this.domains], bytes: this.bytes, states: this.tree,
    };
  }

  async writeBundle() {
    await fsp.mkdir(this.out, { recursive: true });
    await fsp.writeFile(path.join(this.out, 'bundle.json'),
      JSON.stringify({ ...this.state(), finished: new Date().toISOString() }, null, 2));
    await fsp.writeFile(sessionFile(this.name), JSON.stringify(this.state(), null, 2));
  }

  treeText() {
    if (!this.tree.length) return 'состояний пока нет';
    return this.tree.map((n, i) => {
      const via = n.actions.length ? ` — ${n.actions.map((a) => `${a.role}:"${a.name}"`).join(' → ')}` : '';
      return `${String(i + 1).padStart(2)}. [глубина ${n.depth}] ${n.file}${via}`;
    }).join('\n');
  }
}

function optsFrom(flags, positional) {
  return {
    url: positional[0] || process.env.URL || '',
    output: flags.output ?? process.env.OUTPUT_DIR ?? './output',
    depth: flags.depth ?? process.env.DEPTH ?? 3,
    maxStates: flags.maxStates ?? process.env.MAX_STATES ?? 40,
    perState: flags.perState ?? process.env.PER_STATE ?? 10,
    maxPerTemplate: flags.maxPerTemplate ?? process.env.MAX_PER_TEMPLATE ?? 2,
    wait: flags.wait ?? process.env.WAIT_MS ?? 1200,
    viewport: flags.viewport ?? process.env.VIEWPORT ?? '1440x900',
    include: flags.include ?? process.env.INCLUDE ?? '',
    exclude: flags.exclude ?? process.env.EXCLUDE ?? '',
    headless: flags.headless !== undefined ? flags.headless : process.env.HEADLESS !== 'false',
  };
}

function send(port, payload, timeoutMs = 1800000) {
  return new Promise((resolve, reject) => {
    const sock = net.createConnection({ port, host: '127.0.0.1' });
    let buf = '';
    const timer = setTimeout(() => { sock.destroy(); reject(new Error('демон не ответил')); }, timeoutMs);
    sock.on('connect', () => sock.write(`${JSON.stringify(payload)}\n`));
    sock.on('data', (d) => {
      buf += d.toString();
      const i = buf.indexOf('\n');
      if (i < 0) return;
      clearTimeout(timer);
      sock.end();
      try { resolve(JSON.parse(buf.slice(0, i))); } catch (e) { reject(e); }
    });
    sock.on('error', (e) => { clearTimeout(timer); reject(e); });
  });
}

function readSession(name) {
  try { return JSON.parse(fs.readFileSync(sessionFile(name), 'utf8')); } catch { return null; }
}

async function daemon(port, session, flags = {}) {
  fs.mkdirSync(SESSION_DIR, { recursive: true });
  const cap = new Capture(optsFrom(flags, [flags.url || process.env.URL || '']));
  cap.name = session;
  await cap.launch();
  await cap.open();

  const server = net.createServer((sock) => {
    let buf = '';
    sock.on('data', async (d) => {
      buf += d.toString();
      const i = buf.indexOf('\n');
      if (i < 0) return;
      const line = buf.slice(0, i);
      buf = buf.slice(i + 1);
      let msg = {};
      try { msg = JSON.parse(line); } catch { /* пусто */ }
      let reply = { ok: true };
      try {
        if (msg.cmd === 'auto') reply.text = await cap.auto();
        else if (msg.cmd === 'snap') reply.text = await cap.snap(msg.label);
        else if (msg.cmd === 'click') reply.text = await cap.clickCmd(msg.role, msg.name);
        else if (msg.cmd === 'fill') reply.text = await cap.fillCmd(msg.role, msg.name, msg.value);
        else if (msg.cmd === 'goto') reply.text = await cap.goto(msg.url);
        else if (msg.cmd === 'status') reply.text = `${cap.url} | ${cap.summary()}`;
        else if (msg.cmd === 'tree') reply.text = cap.treeText();
        else if (msg.cmd === 'done') {
          await cap.writeBundle();
          reply.text = `готово: ${cap.summary()}\nSpine: ${cap.counts()}`;
          await cap.browser.close();
          server.close();
          setTimeout(() => process.exit(0), 300);
        } else if (msg.cmd === 'kill') {
          await cap.browser.close().catch(() => {});
          server.close();
          setTimeout(() => process.exit(0), 200);
        } else reply = { ok: false, text: `неизвестная команда: ${msg.cmd}` };
      } catch (e) { reply = { ok: false, text: e.message }; }
      try { sock.write(`${JSON.stringify(reply)}\n`); } catch { /* клиент ушёл */ }
    });
  });
  await new Promise((res) => server.listen(port, '127.0.0.1', res));
  fs.writeFileSync(sessionFile(session), JSON.stringify({ ...cap.state(), port, pid: process.pid }, null, 2));
  log(`демон websnap слушает 127.0.0.1:${port} (сессия ${session})`);
}

async function main() {
  const argv = process.argv.slice(2);
  if (argv[0] === '__daemon') {
    const { flags } = parseArgs(argv.slice(1));
    keepAlive = true;
    await daemon(parseInt(flags.port, 10), String(flags.session || 'default'), flags);
    return;
  }

  const { flags, positional } = parseArgs(argv);
  const cmd = positional[0] || 'help';
  const session = String(flags.session || process.env.SESSION || 'default');
  const st = readSession(session);
  const port = parseInt(flags.port || (st && st.port) || 0, 10);

  if (cmd === 'help' || flags.help) {
    log(`websnap-подобный обход SPA

  open <url> [--output ./site] [--viewport 1440x900] [--wait 1500] [--no-headless]
  auto [--depth 3] [--max-states 40] [--include "nav a"] [--exclude "footer *"]
  tree | status | done | kill
  snap [--label name] | click <role> <name> | fill <role> <name> <value> | goto <url>

  Без open команда auto выполняет весь цикл в одном процессе.`);
    return;
  }

  if (cmd === 'open') {
    const opts = optsFrom(flags, [positional[1] || process.env.URL || '']);
    if (!opts.url) throw new Error('нужен URL: open <url>');
    const p = parseInt(flags.port || '0', 10) || 45000 + Math.floor(Math.random() * 5000);
    const child = spawn(process.execPath, [SELF, '__daemon', '--port', String(p), '--session', session,
      '--url', opts.url, '--output', opts.output, '--viewport', opts.viewport, '--wait', String(opts.wait),
      '--depth', String(opts.depth), '--max-states', String(opts.maxStates),
      '--include', opts.include, '--exclude', opts.exclude, ...(opts.headless ? [] : ['--no-headless'])],
    { stdio: 'inherit', detached: false });
    child.unref();
    for (let i = 0; i < 120; i++) {
      await sleep(500);
      const s = readSession(session);
      if (s && s.port) { log(`сессия ${session} готова (порт ${s.port})`); return; }
    }
    throw new Error('демон не поднялся');
  }

  if (cmd === 'tree' && st && st.states) { log(st.states.map((n, i) => `${i + 1}. [глубина ${n.depth}] ${n.file}`).join('\n')); return; }

  if (port) {
    const reply = await send(port, { cmd, role: positional[1], name: positional[2], value: positional[3], label: flags.label, url: positional[1] && cmd === 'goto' ? positional[1] : undefined });
    log(reply.text || (reply.ok ? 'ок' : 'ошибка'));
    if (!reply.ok) process.exitCode = 1;
    return;
  }

  if (cmd === 'auto' || cmd === 'done') {
    const opts = optsFrom(flags, [st && st.url || process.env.URL || '']);
    if (!opts.url) throw new Error('нет URL: запусти open <url> или задай URL');
    const cap = new Capture(opts);
    await fsp.mkdir(cap.out, { recursive: true });
    await cap.launch();
    await cap.open();
    log(cap.summary());
    await cap.auto();
    await cap.writeBundle();
    log(`готово за ${since()}: ${cap.summary()}`);
    log(`Spine: ${cap.counts()}`);
    await cap.browser.close();
    return;
  }

  throw new Error(`неизвестная команда: ${cmd} (см. --help)`);
}

main()
  .then(() => { if (!keepAlive) process.exit(process.exitCode || 0); })
  .catch((e) => { console.error('ошибка websnap:', e.message); process.exit(1); });
