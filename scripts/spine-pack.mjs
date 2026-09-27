#!/usr/bin/env node
// Упаковщик скачанных ресурсов Spine в структуру res/spine/<набор>/
// Использование: node scripts/spine-pack.mjs <srcDir> [outDir]
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { stripQuerySuffix } from './download-resources.mjs';

const NOISE = [
  /cdn-cgi\/challenge/i,
  /challenges\.cloudflare\.com/i,
  /googletagmanager\.com/i,
  /ipify\.org/i,
];

const isNoise = (p) => NOISE.some((re) => re.test(p));

const stripExt = (n) => path.basename(n, path.extname(n));

const exists = (p) => {
  try {
    fs.accessSync(p);
    return true;
  } catch {
    return false;
  }
};

const copy = (from, to) => {
  fs.mkdirSync(path.dirname(to), { recursive: true });
  fs.copyFileSync(from, to);
  return to;
};

function walk(dir, acc = []) {
  let items = [];
  try {
    items = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return acc;
  }
  for (const it of items) {
    const p = path.join(dir, it.name);
    if (it.isDirectory()) walk(p, acc);
    else if (it.isFile()) acc.push(p);
  }
  return acc;
}

// Spine-скелет в JSON: маркеры skeleton/bones/slots/animations.
// Читаем только начало файла — скелеты бывают мегабайтными, а json.parse
// всего файла ещё и падает на бинарнике с расширением .json.
function isSpineSkeleton(p) {
  let head = '';
  try {
    const fd = fs.openSync(p, 'r');
    const buf = Buffer.alloc(65536);
    const n = fs.readSync(fd, buf, 0, buf.length, 0);
    fs.closeSync(fd);
    head = buf.subarray(0, n).toString('utf8');
  } catch {
    return false;
  }
  if (!head.trimStart().startsWith('{')) return false;
  return /"\s*(skeleton|bones|slots|animations)\s*":/.test(head);
}

// Текстуры из .atlas: строки вида "  name.png" / "page.png" (без отступа = page)
function parseAtlasPages(atlasPath) {
  let text = '';
  try {
    text = fs.readFileSync(atlasPath, 'utf8');
  } catch {
    return [];
  }
  const pages = [];
  for (const line of text.split(/\r?\n/)) {
    if (!line || line.startsWith(' ')) continue;
    const head = line.split(/\s+/)[0];
    if (head && /\.(png|jpg|jpeg|webp)$/i.test(head)) pages.push(head);
  }
  return pages;
}

function findTextureByBasename(filesIndex, pageName) {
  const base = stripExt(pageName);
  const candidates = filesIndex.get(base) || [];
  if (!candidates.length) {
    const alt = base.replace(/[_-]+/g, '');
    return (filesIndex.get(alt) || [])[0] || null;
  }
  // предпочитаем путь, где есть сегмент "images" (типичная раскладка исходников)
  return candidates.find((p) => /(^|[\\/])images?[\\/]/i.test(p)) || candidates[0];
}

/**
 * @param {string} srcDir  каталог со скачанными ресурсами
 * @param {string} outDir  корень, внутри которого создаётся res/spine/<набор>/
 * @param {{setName?:(s:string)=>string, quiet?:boolean}} [opts]
 */
export function packSpine(srcDir, outDir, opts = {}) {
  const setName = opts.setName || ((s) => s);
  const src = path.resolve(srcDir);
  const out = path.resolve(outDir);
  const result = { sets: [], files: 0, skipped: 0, incomplete: [] };

  if (!exists(src)) {
    result.skipped++;
    return result;
  }

  const all = walk(src).filter((p) => !isNoise(p));

  // индекс: basename без расширения -> все пути
  const index = new Map();
  for (const p of all) {
    const b = stripExt(p);
    if (!index.has(b)) index.set(b, []);
    index.get(b).push(p);
  }

  const atlases = [];
  const skels = [];
  const atlasNames = new Set();
  for (const p of all) {
    if (/\.atlas$/i.test(p)) { atlases.push(p); atlasNames.add(stripExt(stripQuerySuffix(path.basename(p)))); }
  }
  for (const p of all) {
    if (/\.atlas$/i.test(p)) continue;
    if (/\.(skel|bin)$/i.test(p)) { skels.push(p); continue; }
    // PG Soft и подобные отдают скелеты Spine как .json (main_resources011.json).
    // Считаем скелетом либо по маркерам Spine, либо если рядом есть одноимённый .atlas
    if (/\.json$/i.test(p)) {
      const key = stripExt(stripQuerySuffix(path.basename(p)));
      if (isSpineSkeleton(p) || atlasNames.has(key)) skels.push(p);
    }
  }

  // сопоставление по basename БЕЗ суффикса -<10 символов>
  const skelKeys = new Map();
  for (const p of skels) {
    const key = stripExt(stripQuerySuffix(path.basename(p)));
    if (!skelKeys.has(key)) skelKeys.set(key, []);
    skelKeys.get(key).push(p);
  }
  const usedSkels = new Set();

  const taken = new Set();
  for (const a of atlases) {
    const name = stripExt(stripQuerySuffix(path.basename(a)));
    if (taken.has(name)) continue;
    const matches = (skelKeys.get(name) || []).filter((s) => !usedSkels.has(s));
    if (!matches.length) {
      // ничего не выбрасываем: неполную пару тоже раскладываем в _incomplete
      const dir = path.join(out, 'res', 'spine', '_incomplete', setName(name));
      const files = [];
      const sibs = all.filter((q) => stripExt(stripQuerySuffix(path.basename(q))) === name);
      for (const q of sibs) {
        const dst = path.join(dir, path.basename(stripQuerySuffix(q)));
        if (exists(dst)) continue;
        files.push(copy(q, dst));
      }
      if (!files.length) files.push(copy(a, path.join(dir, path.basename(stripQuerySuffix(a)))));
      result.files += files.length;
      result.incomplete.push({ atlas: a, reason: 'нет пары скелета', dir, files: files.length });
      result.skipped++;
      taken.add(name);
      continue;
    }
    matches.sort();
    const skel = matches[0];
    usedSkels.add(skel);
    taken.add(name);

    const dir = path.join(out, 'res', 'spine', setName(name));
    const files = [copy(a, path.join(dir, path.basename(stripQuerySuffix(a)))), copy(skel, path.join(dir, path.basename(stripQuerySuffix(skel))))];

    const imgDir = path.join(dir, 'images', path.basename(a, '.atlas'));
    const pages = parseAtlasPages(a);
    const used = new Set();
    if (pages.length) {
      for (const page of pages) {
        const srcImg = findTextureByBasename(index, page);
        if (srcImg) {
          files.push(copy(srcImg, path.join(imgDir, path.basename(page))));
          used.add(srcImg);
        } else {
          const keep = path.join(imgDir, '.keep');
          if (!exists(keep)) {
            fs.mkdirSync(imgDir, { recursive: true });
            fs.writeFileSync(keep, '');
            files.push(keep);
          }
        }
      }
    } else {
      const keep = path.join(imgDir, '.keep');
      fs.mkdirSync(imgDir, { recursive: true });
      fs.writeFileSync(keep, '');
      files.push(keep);
    }

    result.files += files.length;
    result.sets.push({ name, dir, files, images: pages.length });
  }

  for (const [key, list] of skelKeys) {
    for (const s of list) {
      if (usedSkels.has(s)) continue;
      const dir = path.join(out, 'res', 'spine', '_incomplete', setName(key));
      const dst = path.join(dir, path.basename(stripQuerySuffix(s)));
      if (!exists(dst)) { copy(s, dst); result.files++; }
      result.incomplete.push({ skel: s, reason: `нет пары .atlas (ключ ${key})`, dir });
      result.skipped++;
    }
  }

  return result;
}

const invokedDirectly = () => {
  const argv1 = process.argv[1] || '';
  return argv1.endsWith('spine-pack.mjs');
};

if (invokedDirectly()) {
  const [srcDir = '.', outDir = '.'] = process.argv.slice(2);
  console.log(`[spine-pack] Источник: ${path.resolve(srcDir)}`);
  console.log(`[spine-pack] Назначение: ${path.resolve(outDir)}/res/spine`);
  const r = packSpine(srcDir, outDir);
  console.log(`[spine-pack] Наборов: ${r.sets.length}`);
  for (const s of r.sets) console.log(`  - ${s.name}: ${s.files.length} файлов, страниц: ${s.images}`);
  console.log(`[spine-pack] Всего файлов: ${r.files}, пропущено: ${r.skipped}, неполных: ${r.incomplete.length}`);
  if (r.incomplete.length) {
    console.log('[spine-pack] Неполные пары:');
    for (const i of r.incomplete) console.log(`  ! ${i.atlas || i.skel} — ${i.reason}`);
  }
  if (!r.sets.length) console.log('[spine-pack] Ничего не упаковано.');
}

export default packSpine;
export { fileURLToPath };
