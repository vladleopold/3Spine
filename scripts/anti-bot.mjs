#!/usr/bin/env node
// anti-bot.mjs — определение блокировок (Cloudflare Turnstile, гео-блок, JS-challenge)
// и подсказки следующих действий. Чистые функции, без сетевых вызовов.

import { fileURLToPath } from 'node:url';

/* ---------- внутренние помощники ---------- */

const CF_CHALLENGE_HOST_RE = /(^|\.)challenges\.cloudflare\.com$/i;
const CF_CHALLENGE_PATH_RE = /\/cdn-cgi\/(challenge-platform|trace|l)|__cf_chl/i;
const BLOCK_HOST_RE = /^(restriction|restricted|geo|blocked|deny|denied|forbidden|access)[.-]/i;
const BLOCK_PATH_RE = /^\/(restriction|restricted|geo|blocked|deny|denied|forbidden|unavailable)[/-]/i;
const GAME_URL_RE = /(game|play|launch|demo|slot|casino|gamemanager|gamehub|gamecenter)/i;
const DEMO_QUERY_RE = /(^|[?&])(demo|mode)=(1|true|yes|on|demo)(&|$)/i;
const IFRAME_RE = /<iframe/i;
const SPINE_RE = /spine|spine-?runtime|skeleton|atlas|animation|\.json|\.atlas|\.skel/i;

/** Нормализация хоста: без схемы, без www, нижний регистр. */
function normalizeHost(input) {
  if (typeof input !== 'string' || input.trim() === '') return '';
  let value = input.trim();
  if (value.includes('//')) {
    try {
      value = new URL(value).host;
    } catch {
      value = value.replace(/^[a-z]+:\/\//i, '');
    }
  }
  value = value.split('/')[0].split('?')[0].split('#')[0].split(':')[0];
  return value.replace(/^www\./i, '').toLowerCase();
}

function toArray(value) {
  if (value === undefined || value === null) return [];
  return Array.isArray(value) ? value : [value];
}

function toText(value) {
  if (typeof value === 'string') return value;
  if (value === undefined || value === null) return '';
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function pushUnique(list, value) {
  if (!value) return;
  const item = String(value);
  if (!list.includes(item)) list.push(item);
}

function parseUrlSafe(url) {
  try {
    return new URL(String(url));
  } catch {
    return null;
  }
}

/* ---------- 1. detectBlock ---------- */

/**
 * Определяет тип блокировки страницы.
 * @param {string} pageUrl      Финальный URL страницы после редиректов
 * @param {string|string[]} hosts Хост(ы), которые считаем «своими» (загружены нормально)
 * @param {string|string[]} savedPaths Ожидаемые пути страниц со spine/контентом
 * @returns {{blocked: boolean, reason: 'turnstile'|'geoblock'|'cf-challenge'|'none', evidence: string[]}}
 */
export function detectBlock(pageUrl, hosts, savedPaths) {
  const evidence = [];
  const url = String(pageUrl ?? '');
  const parsed = parseUrlSafe(url);
  const hostList = toArray(hosts)
    .map((h) => normalizeHost(h))
    .filter(Boolean);
  const pathList = toArray(savedPaths).map((p) => toText(p).trim()).filter(Boolean);

  if (!url) {
    return { blocked: true, reason: 'cf-challenge', evidence: ['пустой URL страницы'] };
  }

  // Признак 1: Cloudflare Turnstile / challenge-platform в URL
  const hostIsCfChallenge = hostList.some((h) => CF_CHALLENGE_HOST_RE.test(h));
  const pageHostIsCfChallenge = parsed ? CF_CHALLENGE_HOST_RE.test(parsed.hostname) : false;
  const pathIsCfChallenge = CF_CHALLENGE_PATH_RE.test(parsed ? parsed.pathname + parsed.search : url);

  if (hostIsCfChallenge) {
    pushUnique(evidence, `хост из списка — challenges.cloudflare.com: ${hostList.join(', ')}`);
  }
  if (pageHostIsCfChallenge) {
    pushUnique(evidence, `финальный хост — ${parsed.hostname} (challenges.cloudflare.com)`);
  }
  if (pathIsCfChallenge) {
    pushUnique(evidence, `в URL найден cdn-cgi/challenge-platform: ${url}`);
  }
  if (/turnstile|g-recaptcha|__cf_chl/i.test(url)) {
    pushUnique(evidence, `в URL встречен маркер turnstile/cf-challenge: ${url}`);
  }

  if (evidence.length > 0) {
    return { blocked: true, reason: 'turnstile', evidence };
  }

  // Признак 2: гео/доступная блокировка по финальному URL
  const finalHost = parsed ? normalizeHost(parsed.hostname) : normalizeHost(url);
  const finalPath = parsed ? parsed.pathname : url;

  if (BLOCK_HOST_RE.test(finalHost)) {
    pushUnique(evidence, `финальный хост похож на блокировку: ${finalHost}`);
  }
  if (BLOCK_PATH_RE.test(finalPath)) {
    pushUnique(evidence, `финальный путь похож на блокировку: ${finalPath}`);
  }
  if (finalHost && !hostList.includes(finalHost) && /restriction|blocked|denied/i.test(finalHost)) {
    pushUnique(evidence, `финальный хост не в списке доверенных и содержит restriction/blocked: ${finalHost}`);
  }

  if (evidence.length > 0) {
    return { blocked: true, reason: 'geoblock', evidence };
  }

  // Признак 3: cf-challenge — ушли с ожидаемого хоста/пути, целевого контента нет
  const expectedPathHit = pathList.some(
    (p) => p.length > 1 && (finalPath.includes(p) || url.includes(p)),
  );
  const hostMatch =
    hostList.length === 0
      ? true
      : hostList.includes(finalHost) || (parsed && hostList.includes(normalizeHost(parsed.hostname)));
  const text = pathList.map(toText).join(' ') + ' ' + url;
  const noSpine = !SPINE_RE.test(text);

  if (!expectedPathHit && pathList.length > 0) {
    pushUnique(
      evidence,
      `финальный URL ${url} не содержит ни одного сохранённого пути: ${pathList.join(', ')}`,
    );
  }
  if (!hostMatch) {
    pushUnique(
      evidence,
      `финальный хост ${finalHost} не совпадает с доверенными: ${hostList.join(', ') || '—'}`,
    );
  }
  if (noSpine) {
    pushUnique(evidence, 'в URL нет признаков spine/atlas/анимации');
  }
  if (IFRAME_RE.test(text)) {
    pushUnique(evidence, 'в разметке/адресе есть iframe, но нет spine-контента');
  }

  if (evidence.length > 0) {
    return { blocked: true, reason: 'cf-challenge', evidence };
  }

  return { blocked: false, reason: 'none', evidence: ['блокировка не обнаружена'] };
}

/* ---------- 2. looksLikeGameUrl ---------- */

/**
 * Похоже ли URL на страницу игрового автомата/демо.
 * @param {string} url
 * @returns {boolean}
 */
export function looksLikeGameUrl(url) {
  const raw = String(url ?? '').trim();
  if (!raw) return false;

  const parsed = parseUrlSafe(raw);
  const host = parsed ? parsed.hostname : raw;
  const pathAndQuery = parsed ? `${parsed.pathname}${parsed.search}` : raw;

  if (GAME_URL_RE.test(host)) return true;
  if (GAME_URL_RE.test(pathAndQuery)) return true;
  if (DEMO_QUERY_RE.test(parsed ? parsed.search : raw)) return true;

  return false;
}

/* ---------- 3. suggestNextActions ---------- */

const REASON_RULES = {
  turnstile: [
    'Не использовать headless-подобные режимы: запустить Chromium в headed-режиме и убрать флаги --disable-blink-features=AutomationControlled.',
    'Пропустить cookie/токен Turnstile один раз вручную, затем переиспользовать сохранённую сессию (userDataDir) во всех прогонах.',
    'Снизить частоту запросов до одного прогона в N секунд и добавить паузу перед навигацией на целевой URL.',
  ],
  geoblock: [
    'Проверить код ответа и заголовки (HTTP 403/451 + cf-mitigated), чтобы отличить гео-блок от бан-IP.',
    'Сменить egress-IP/прокси на регион, где игра доступна, и повторить запрос.',
    'Закрепить за прокси постоянную сессию: IP + User-Agent + cookies не должны меняться между прогонами.',
  ],
  'cf-challenge': [
    'Сохранить финальный URL после редиректов и HTML страницы в отчёт — проверить, был ли редирект на challenge.',
    'Отключить перехват challenge-маршрутов (/cdn-cgi/challenge-platform) в прокси-логгере, чтобы не терять реальную страницу.',
    'Дождаться завершения JS-challenge через networkidle перед выгрузкой HTML, иначе сохраняется пустая заглушка.',
  ],
  none: [
    'Зафиксировать успешный профиль: URL, куки, User-Agent — использовать как эталон для остальных страниц.',
    'Добавить проверку looksLikeGameUrl на найденные URL, чтобы отделять страницы игры от заглушек.',
    'Расширить список сохранённых путей (savedPaths) реальными маршрутами spine-контента.',
  ],
};

const EXTRA_RULES = [
  {
    match: (report) => Array.isArray(report?.evidence) && report.evidence.length > 3,
    action: 'Слишком много улик сразу: собрать детальный отчёт по одному URL, чтобы локализовать причину.',
  },
  {
    match: (report) => looksLikeGameUrl(report?.url),
    action: 'URL похож на игру: качать контент только из белого списка доменов и не обходить лицензионные ограничения.',
  },
  {
    match: (report) => looksLikeGameUrl(report?.expectedUrl) !== looksLikeGameUrl(report?.url),
    action: 'Сравнить ожидаемый и фактический URL: подтвердить, что редирект увёл на другую страницу.',
  },
  {
    match: (report) => !report?.url,
    action: 'URL не задан: добавить валидацию входных данных до сетевого вызова.',
  },
];

/**
 * Возвращает 3–5 конкретных действий по отчёту detectBlock.
 * @param {{url?: string, expectedUrl?: string, blocked?: boolean, reason?: string, evidence?: string[]}} report
 * @returns {string[]}
 */
export function suggestNextActions(report) {
  const data = report && typeof report === 'object' ? report : {};
  const reason = EXTRA_RULES.some((r) => r.match(data)) ? undefined : undefined;
  const base = REASON_RULES[data.reason] ? [...REASON_RULES[data.reason]] : [...REASON_RULES['cf-challenge']];

  for (const rule of EXTRA_RULES) {
    if (base.length >= 5) break;
    if (rule.match(data)) base.push(rule.action);
  }

  void reason;

  const seen = new Set();
  return base
    .filter((a) => (seen.has(a) ? false : (seen.add(a), true)))
    .slice(0, 5);
}

/* ---------- демо-вывод при прямом запуске ---------- */

function demo() {
  const cases = [
    {
      label: 'Turnstile / challenge-platform',
      url: 'https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1?ray=8ab',
      hosts: ['spine.example.com'],
      savedPaths: ['/game/load', '/spine/player'],
    },
    {
      label: 'Гео-блокировка',
      url: 'https://restriction.example.com/geo/blocked?country=RU',
      hosts: ['spine.example.com'],
      savedPaths: ['/game/load'],
    },
    {
      label: 'JS-challenge (ушли со своего хоста, spine нет)',
      url: 'https://cdn-cgi.lb.example.net/challenge',
      hosts: ['spine.example.com'],
      savedPaths: ['/game/load', '/spine/player.json'],
    },
    {
      label: 'Чистый доступ',
      url: 'https://spine.example.com/game/load?skin=default',
      hosts: ['spine.example.com'],
      savedPaths: ['/game/load'],
    },
  ];

  console.log('=== detectBlock ===');
  for (const c of cases) {
    const result = detectBlock(c.url, c.hosts, c.savedPaths);
    console.log(`\n[${c.label}]`);
    console.log(`  URL: ${c.url}`);
    console.log(`  blocked=${result.blocked} reason=${result.reason}`);
    for (const e of result.evidence) console.log(`   - ${e}`);
    const actions = suggestNextActions({ ...result, url: c.url, expectedUrl: c.savedPaths[0] });
    console.log('  Следующие действия:');
    actions.forEach((a, i) => console.log(`   ${i + 1}. ${a}`));
  }

  console.log('\n=== looksLikeGameUrl ===');
  const urls = [
    'https://site.com/game/index.html',
    'https://site.com/play/slot777',
    'https://site.com/launch?demo=1',
    'https://site.com/player?id=42',
    'https://site.com/?mode=demo',
  ];
  for (const u of urls) {
    console.log(`  ${looksLikeGameUrl(u) ? 'да ' : 'нет'}  ${u}`);
  }
}

const invokedDirectly =
  process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1];

if (invokedDirectly) {
  demo();
}
