// Ядро сейвера: разбор имён Spine-файлов, подсчёт пар, пути и манифесты.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';
import {
  sanitizePath, harvest, countSpine, extOf, stripQuerySuffix,
} from '../scripts/download-resources.mjs';

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'core-'));
const write = (rel, body) => {
  const p = path.join(...rel.split('/'));
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, body);
};

test('stripQuerySuffix: снимает 10-символьный query-суффикс', () => {
  assert.equal(stripQuerySuffix('arrow-PzdlMjBlM2.atlas'), 'arrow.atlas');
  assert.equal(stripQuerySuffix('x.skel'), 'x.skel');
  assert.equal(stripQuerySuffix('a_b-c-d1e2f3g4.json'), 'a_b-c.json');
});

test('stripQuerySuffix: короткий хвост и его отсутствие не трогаются', () => {
  assert.equal(stripQuerySuffix('hero-v2.json'), 'hero-v2.json');
  assert.equal(stripQuerySuffix('a-b-cdefgh.png'), 'a-b.png');
  assert.equal(stripQuerySuffix('spine'), 'spine');
});

test('countSpine: 3oaks — atlas и skel с РАЗНЫМИ query дают одну пару', () => {
  const d = tmp();
  write('arrow-PzdlMjBlM2.atlas', 'atlas');
  write('arrow-Pzg0NTc5Mz.skel', 'skel');
  write('y.atlas', 'atlas');     // без бинаря
  write('z.skel', 'skel');       // без атласа
  assert.equal(countSpine(d), 1);
});

test('countSpine: полная пара в подкаталоге images/hero', () => {
  const d = tmp();
  write('images/hero/hero.atlas', 'atlas');
  write('images/hero/hero.bin', 'bin');
  assert.equal(countSpine(d), 1);
});

test('countSpine: одинаковые имена в разных папках парой не считаются', () => {
  const d = tmp();
  write('images/hero/hero.atlas', 'atlas');
  write('hero.skel', 'skel');
  assert.equal(countSpine(d), 0);
});

test('sanitizePath: query даёт суффикс из 10 символов, расширение сохранено', () => {
  const p = sanitizePath('https://cdn.3oaks.com/game/arrow.atlas?v=741ff9e7', '/out');
  assert.ok(p.startsWith(path.join('/out')));
  assert.equal(p, path.join('/out', 'cdn.3oaks.com', 'game/arrow-P3Y9NzQxZm.atlas'));
  assert.equal(path.extname(p), '.atlas');
  assert.match(path.basename(p), /^arrow-[A-Za-z0-9_-]{10}\.atlas$/);
});

test('sanitizePath: без query имя файла не меняется', () => {
  assert.equal(sanitizePath('https://cdn.3oaks.com/g/hero.skel', '/out'),
    path.join('/out', 'cdn.3oaks.com', 'g/hero.skel'));
});

test('harvest: пары uuid -> png/atlas/json относительно base', () => {
  const out = new Set(), seen = new Set();
  harvest('{"a1b2c3d4-1111-2222-3333-444455556666":"images/ui/bg.png",'
    + '"deadbeef-9999-8888-7777-666655554444":"data/hero.atlas",'
    + '"0f0f0f0f-1111-2222-3333-444455556666":"cfg/tables.json"}',
    'https://cdn.3oaks.com/game/manifest.json', 2, out, seen);
  assert.equal(out.size, 3);
  assert.ok(out.has('https://cdn.3oaks.com/game/images/ui/bg.png'));
  assert.ok(out.has('https://cdn.3oaks.com/game/data/hero.atlas'));
  assert.ok(out.has('https://cdn.3oaks.com/game/cfg/tables.json'));
});

test('harvest: абсолютные URL не переписываются', () => {
  const out = new Set(), seen = new Set();
  harvest('{"11111111-2222-3333-4444-555555555555":"https://other.cdn.io/a/b.png"}',
    'https://cdn.3oaks.com/game/manifest.json', 2, out, seen);
  assert.equal(out.size, 1);
  assert.ok(out.has('https://other.cdn.io/a/b.png'));
});

test('harvest: мусор (index.html, пробелы) игнорируется', () => {
  const out = new Set(), seen = new Set();
  harvest('{"11111111-2222-3333-4444-555555555555":"index.html",'
    + '"22222222-3333-4444-5555-666666666666":"a b c",'
    + '"x":"images/ok.png"}',
    'https://cdn.3oaks.com/', 2, out, seen);
  assert.equal(out.size, 0);
});

test('harvest: массивы files/paths разбираются, чужое расширение — нет', () => {
  const out = new Set(), seen = new Set();
  harvest('{"files":["res/a.png","res/b.atlas","res/c.txt"]}',
    'https://cdn.3oaks.com/g/', 2, out, seen);
  assert.equal(out.size, 2);
  assert.ok(out.has('https://cdn.3oaks.com/g/res/a.png'));
  assert.ok(out.has('https://cdn.3oaks.com/g/res/b.atlas'));
});

test('harvest: повторы не дублируются', () => {
  const out = new Set(), seen = new Set();
  harvest('{"11111111-2222-3333-4444-555555555555":"data/hero.atlas",'
    + '"22222222-3333-4444-5555-666666666666":"./data/hero.atlas"}',
    'https://cdn.3oaks.com/g/', 2, out, seen);
  assert.equal(out.size, 1);
});

test('extOf: расширение из pathname, query игнорируется, регистр вниз', () => {
  assert.equal(extOf('https://cdn.3oaks.com/game/arrow.atlas?v=741ff9e7'), '.atlas');
  assert.equal(extOf('https://cdn.3oaks.com/game/hero.bin'), '.bin');
  assert.equal(extOf('https://cdn.3oaks.com/game/hero.SKEL'), '.skel');
  assert.equal(extOf('https://cdn.3oaks.com/game/main.js?x=1'), '.js');
  assert.equal(extOf('не url'), '');
});
