#!/usr/bin/env node
// Отчёт по содержимому каталога загрузок: файлы, spine-пары, мусор, incomplete.
// Использование: node scripts/report.mjs <dir>
// GITHUB_STEP_SUMMARY/toMarkdown — таблица для сводки шага.

import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { stripQuerySuffix } from './download-resources.mjs';

const EXTS = {
  atlas: /\.atlas$/i,
  skel: /\.skel$/i,
  bin: /\.bin$/i,
  json: /\.json$/i,
  image: /\.(png|jpe?g|webp|gif|bmp|tga)$/i,
  wasm: /\.wasm$/i,
  font: /\.(ttf|otf|woff2?)$/i,
};

// хосты, чей контент — мусор (CDN-защита, трекинг, ipify)
const NOISE_HOSTS = [
  'cdn-cgi',
  'challenge',
  'challenges.cloudflare.com',
  'googletagmanager',
  'ipify',
];

function isNoise(file) {
  const f = file.toLowerCase();
  return NOISE_HOSTS.some((h) => f.includes(h));
}

function isSkippable(file) {
  const f = file.toLowerCase();
  return f.endsWith('.zip') || f === 'saver-report.json';
}

function categorize(file) {
  for (const [k, re] of Object.entries(EXTS)) if (re.test(file)) return k;
  return null;
}

function hostOf(file) {
  const parts = file.split(/[\\/]/);
  return parts.length > 1 ? parts[0] : null;
}

/**
 * Рекурсивный подсчёт по каталогу.
 * @param {string} dir
 * @returns {{files:number,spine:number,atlas:number,skel:number,bin:number,json:number,
 *   images:number,wasm:number,fonts:number,hosts:string[],noise:number,
 *   incomplete:{missingBin:string[],missingAtlas:string[]}}}
 */
export function summarize(dir) {
  const res = {
    files: 0,
    spine: 0,
    atlas: 0,
    skel: 0,
    bin: 0,
    json: 0,
    images: 0,
    wasm: 0,
    fonts: 0,
    hosts: [],
    noise: 0,
    incomplete: { missingBin: [], missingAtlas: [] },
  };
  if (!dir || !fs.existsSync(dir) || !fs.statSync(dir).isDirectory()) return res;

  const hostTally = new Map();
  const keyOf = (full) => {
    const k = path.join(path.dirname(full), stripQuerySuffix(path.basename(full)));
    return k.replace(/\.(atlas|skel|bin)$/i, '');
  };
  const atlases = new Set();
  const skels = new Set();

  const walk = (d) => {
    for (const it of fs.readdirSync(d, { withFileTypes: true })) {
      const full = path.join(d, it.name);
      if (it.isDirectory()) {
        walk(full);
        continue;
      }
      if (!it.isFile() || it.isSymbolicLink()) continue;
      if (isSkippable(it.name)) continue;

      if (isNoise(full)) {
        res.noise++;
        const h = hostOf(full);
        if (h) hostTally.set(h, (hostTally.get(h) || 0) + 1);
        continue;
      }

      res.files++;
      const h = hostOf(full);
      if (h) hostTally.set(h, (hostTally.get(h) || 0) + 1);

      const kind = categorize(it.name);
      if (kind === 'atlas') {
        res.atlas++;
        atlases.add(keyOf(full));
      } else if (kind === 'skel') {
        res.skel++;
        skels.add(keyOf(full));
      } else if (kind === 'bin') {
        res.bin++;
        skels.add(keyOf(full));
      } else if (kind) {
        res[kind]++;
      }
    }
  };
  walk(dir);

  for (const k of atlases) {
    if (skels.has(k)) res.spine++;
    else res.incomplete.missingBin.push(path.basename(k));
  }
  for (const k of skels) {
    if (!atlases.has(k)) res.incomplete.missingAtlas.push(path.basename(k));
  }
  res.incomplete.missingBin.sort();
  res.incomplete.missingAtlas.sort();
  res.hosts = [...hostTally.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([host, n]) => `${host} (${n})`);

  return res;
}

function incompleteList(sum) {
  const { missingBin, missingAtlas } = sum.incomplete || { missingBin: [], missingAtlas: [] };
  const out = [];
  if (missingBin.length) out.push(`atlas без бинаря (${missingBin.length}): ${missingBin.join(', ')}`);
  if (missingAtlas.length) out.push(`бинарь без атласа (${missingAtlas.length}): ${missingAtlas.join(', ')}`);
  return out;
}

/** Таблица для $GITHUB_STEP_SUMMARY. */
export function toMarkdown(sum) {
  const rows = [
    ['Файлов', sum.files],
    ['Spine-пар', sum.spine],
    ['atlas', sum.atlas],
    ['skel', sum.skel],
    ['bin', sum.bin],
    ['json', sum.json],
    ['images', sum.images],
    ['wasm', sum.wasm],
    ['fonts', sum.fonts],
    ['мусор (noise)', sum.noise],
  ];
  const inc = incompleteList(sum);
  let md = '### Отчёт по загрузкам\n\n| Метрика | Значение |\n| --- | ---: |\n';
  for (const [k, v] of rows) md += `| ${k} | ${v} |\n`;
  if (sum.hosts.length) {
    md += `\n**Хосты:** ${sum.hosts.join(', ')}\n`;
  }
  if (inc.length) {
    md += '\n**Incomplete:**\n';
    for (const l of inc) md += `- ${l}\n`;
  } else {
    md += '\nIncomplete: нет\n';
  }
  return md;
}

/** Короткий текст. */
export function toText(sum) {
  const inc = incompleteList(sum);
  const parts = [
    `файлов=${sum.files}`,
    `spine=${sum.spine}`,
    `atlas=${sum.atlas}`,
    `skel=${sum.skel}`,
    `bin=${sum.bin}`,
    `json=${sum.json}`,
    `img=${sum.images}`,
    `wasm=${sum.wasm}`,
    `fonts=${sum.fonts}`,
    `noise=${sum.noise}`,
  ];
  let t = parts.join(' ');
  if (sum.hosts.length) t += `\nhosts: ${sum.hosts.join(', ')}`;
  if (inc.length) t += `\nincomplete: ${inc.join(' | ')}`;
  return t;
}

function main(argv) {
  const dir = argv[0];
  if (!dir) {
    process.stderr.write('usage: node scripts/report.mjs <dir>\n');
    process.exit(2);
  }
  const sum = summarize(dir);
  if (process.env.GITHUB_STEP_SUMMARY) {
    try {
      fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, toMarkdown(sum));
    } catch (e) {
      process.stderr.write(`step summary: ${e.message}\n`);
    }
  }
  process.stdout.write(toText(sum) + '\n');
}

if (process.argv[1] && import.meta.url === `file://${path.resolve(process.argv[1])}`) {
  main(process.argv.slice(2));
}
