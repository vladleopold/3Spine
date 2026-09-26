// Проверка чистой логики сейвера без браузера — гоняется в CI на каждом запуске.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { sanitizePath, harvest, countSpine } from '../scripts/download-resources.mjs';

test('путь сохраняет домен и структуру', () => {
  const p = sanitizePath('https://cdn.kendoo.pro/gs/clients/a/knd1/0/index.js', '/out');
  assert.equal(p, path.join('/out', 'cdn.kendoo.pro', 'gs/clients/a/knd1/0/index.js'));
});

test('query-параметры уходят в имя, расширение сохраняется', () => {
  const p = sanitizePath('https://h.io/atlas/x.atlas?v=741ff9e7', '/out');
  assert.match(path.basename(p), /^x-[A-Za-z0-9_-]{1,10}\.atlas$/);
});

test('корень сайта становится index.html', () => {
  assert.ok(sanitizePath('https://site.ua/', '/out').endsWith('index.html'));
});

test('манифест: пары uuid -> путь', () => {
  const out = new Set(), seen = new Set();
  harvest('{"a1b2c3d4-1111-2222-3333-444455556666":"images/ui/bg.png",'
    + '"deadbeef-9999-8888-7777-666655554444":"data/hero.atlas"}',
    'https://cdn.x.io/assets/manifest.json', 2, out, seen);
  assert.ok([...out].includes('https://cdn.x.io/assets/images/ui/bg.png'));
  assert.ok([...out].includes('https://cdn.x.io/assets/data/hero.atlas'));
});

test('манифест: абсолютные URL не переписываются', () => {
  const out = new Set(), seen = new Set();
  harvest('{"11111111-2222-3333-4444-555555555555":"https://other.io/a/b.png"}',
    'https://cdn.x.io/', 2, out, seen);
  assert.ok([...out].includes('https://other.io/a/b.png'));
});

test('мусор в манифесте игнорируется', () => {
  const out = new Set(), seen = new Set();
  harvest('{"11111111-2222-3333-4444-555555555555":"index.html","x":"a b c"}',
    'https://cdn.x.io/', 2, out, seen);
  assert.equal(out.size, 0);
});

test('Spine считается парами atlas+skel', () => {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), 'rs-'));
  fs.mkdirSync(path.join(d, 'a'), { recursive: true });
  fs.mkdirSync(path.join(d, 'b'), { recursive: true });
  fs.writeFileSync(path.join(d, 'a', 'x.atlas'), 'x');
  fs.writeFileSync(path.join(d, 'b', 'x.skel'), 'x');   // без пары atlas
  fs.writeFileSync(path.join(d, 'a', 'y.atlas'), 'y');
  fs.writeFileSync(path.join(d, 'a', 'y.bin'), 'y');
  assert.equal(countSpine(d), 1);
});
