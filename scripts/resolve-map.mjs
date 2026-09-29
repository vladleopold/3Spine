// Static site/URL resolution map for SpineConverter (Node 22 ESM).
// No network, no side effects on import. Run directly to inspect a URL.

const FIRST_UA = {
  api: [
    'https://apiv2.first.ua/games/providers/games',
    'https://apiv2.first.ua/games/demo?provider={provider}&term={term}',
  ],
};

/** @type {Record<string, {api: string[], note?: string}>} */
export const SITE_MAP = {
  'first.ua': {
    ...FIRST_UA,
  },
  'slotcity.ua': {
    api: [], // проверить
  },
  'cosmolot.ua': {
    api: [], // проверить
  },
  'slotor777.ua': {
    api: [], // проверить
  },
  'beton.ua': {
    api: [], // проверить
  },
  '3oaks.com': {
    api: [], // проверить
  },
  'playson.com': {
    api: [], // проверить
  },
};

const GAME_PATH = /\/(game|play|launch|demo|slot|casino|gamemanager)(\/|$|\?|-|_)/i;
const GAME_QUERY = /[?&](demo|mode|launch)=1\b|[?&](demo|mode|launch)=(true|1)\b/i;
const NON_GAME = /\.(css|js|mjs|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|eot|map|xml|json|txt|pdf|zip|rss)(\?|$)/i;

function registrableHost(hostname) {
  return String(hostname || '')
    .toLowerCase()
    .replace(/^www\d?\./, '')
    .replace(/\.$/, '');
}

/** Resolve a site entry for a page URL. */
export function siteFor(pageUrl) {
  let host = '';
  try {
    host = registrableHost(new URL(pageUrl).hostname);
  } catch {
    return { host: '', key: null, site: null, api: [], note: 'unparsable url' };
  }
  const key = Object.keys(SITE_MAP).find((k) => host === k || host.endsWith(`.${k}`)) ?? null;
  const site = key ? SITE_MAP[key] : null;
  return {
    host,
    key,
    site,
    api: site ? site.api : [],
    note: site?.note ?? (key ? undefined : 'unknown host'),
  };
}

/** Candidate API endpoints for a page URL. */
export function apiCandidates(pageUrl) {
  const { api } = siteFor(pageUrl);
  return [...api];
}

/** Known player iframe selectors for a page URL. */
export function iframeSelectors(pageUrl) {
  const { key } = siteFor(pageUrl);
  const base = [
    'iframe',
    'iframe[id*="game" i]',
    'iframe[src*="game" i]',
    'iframe.game-container',
    'iframe#game',
    'iframe.game-frame',
  ];
  if (key === 'first.ua') {
    return [...base, 'iframe#games-iframe', 'iframe[data-game-url]', '.games-holder iframe'];
  }
  return base;
}

/**
 * Heuristic: does this URL look like it leads into a game?
 * @param {string} url
 * @param {Iterable<string>} [capturedUrls]
 */
export function gameUrlHeuristics(url, capturedUrls = []) {
  const results = [];
  const seen = new Set();

  const candidates = [
    ...(Array.isArray(capturedUrls) ? capturedUrls : [...(capturedUrls || [])]),
    url,
  ].filter((u) => typeof u === 'string' && u.trim());

  for (const raw of candidates) {
    let parsed;
    try {
      parsed = new URL(raw, url && /^https?:/i.test(url) ? url : undefined);
    } catch {
      results.push({ url: raw, isGame: false, reason: 'unparsable' });
      continue;
    }
    if (seen.has(parsed.href)) continue;
    seen.add(parsed.href);

    const { pathname, search } = parsed;
    const host = registrableHost(parsed.hostname);
    const reasons = [];

    if (GAME_PATH.test(pathname)) reasons.push('path');
    if (GAME_QUERY.test(search)) reasons.push('query');
    if (/(^|\.)gamemanager\./i.test(host)) reasons.push('host');
    if (/\/gamemanager/i.test(pathname)) reasons.push('gamemanager');

    const isGame = reasons.length > 0 && !NON_GAME.test(pathname);
    if (isGame && NON_GAME.test(pathname)) reasons.length = 0;

    results.push({
      url: parsed.href,
      host,
      isGame,
      reason: isGame ? reasons.join('+') : NON_GAME.test(pathname) ? 'static asset' : 'no match',
    });
  }

  const games = results.filter((r) => r.isGame);
  return {
    results,
    games: games.map((g) => g.url),
    best: games[0] ?? null,
    isGame: games.length > 0,
  };
}

function main() {
  const arg = process.argv[2];
  if (!arg) {
    console.log('usage: node scripts/resolve-map.mjs <pageUrl> [capturedUrl ...]');
    console.log('known hosts:', Object.keys(SITE_MAP).join(', '));
    return;
  }
  const captured = process.argv.slice(3);
  const site = siteFor(arg);
  console.log('pageUrl      :', arg);
  console.log('host         :', site.host || '-');
  console.log('site key     :', site.key ?? '-');
  console.log('api          :', site.api.length ? site.api.join('\n              ') : '(none) проверить');
  console.log('selectors    :', iframeSelectors(arg).join(', '));
  const h = gameUrlHeuristics(arg, captured);
  console.log('game urls    :', h.games.length ? h.games.join('\n              ') : '(none)');
  console.log('best         :', h.best?.url ?? '-');
}

if (process.argv[1] && import.meta.url === `file://${process.argv[1]}`) {
  main();
}
