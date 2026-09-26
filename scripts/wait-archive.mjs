#!/usr/bin/env node
// Отдельный шаг: дождаться архива, который собирает расширение после нажатия
// кнопки «Save All Resources». Только проверка наличия ZIP и его размер.
import fs from 'fs';
import path from 'path';

const OUT = path.resolve(process.env.OUTPUT_DIR || './artifacts');
const WAIT = parseInt(process.env.ARCHIVE_WAIT_MS || '120000', 10);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const t0 = Date.now();
const since = () => `${((Date.now() - t0) / 1000).toFixed(1)}с`;
const log = (...a) => console.log(...a);

function zips() {
  try {
    return fs.readdirSync(OUT).filter((f) => f.endsWith('.zip') && !f.endsWith('.crdownload'));
  } catch { return []; }
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  log(`жду ZIP в ${OUT} до ${WAIT} мс`);
  const dl = Date.now() + WAIT;
  let found = null;
  while (Date.now() < dl) {
    const z = zips();
    if (z.length) {
      // ждём, пока файл перестанет расти
      const p = path.join(OUT, z[0]);
      const s1 = fs.statSync(p).size;
      await sleep(2000);
      const s2 = fs.statSync(p).size;
      if (s1 === s2 && s2 > 0) { found = p; break; }
    }
    await sleep(2000);
  }
  if (!found) {
    log(`ZIP не появился за ${since()}. В каталоге: ${fs.readdirSync(OUT).join(' ') || 'ничего'}`);
    process.exit(1);
  }
  const mb = (fs.statSync(found).size / 1048576).toFixed(2);
  log(`архив готов: ${path.basename(found)} (${mb} МБ) за ${since()}`);
  process.exit(0);
}

main().catch((e) => { console.error('ошибка:', e.message); process.exit(1); });
