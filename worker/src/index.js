// Spine converter broker: сайт -> GitHub Actions -> результат без токена у пользователя.
// Секрет GITHUB_TOKEN живёт в env Worker'а (Cloudflare), в браузер не попадает.
const GH = "https://api.github.com";
const REPO = "vladleopold/3Spine";
const INBOX = "inbox";
const RESULTS = "results";
const MAX_BYTES = 35 * 1024 * 1024;

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  "Access-Control-Allow-Headers": "content-type, authorization, x-spine-token",
  "Access-Control-Max-Age": "86400",
};

function json(data, status = 200, extra = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...CORS, ...extra },
  });
}

async function j(env, method, path, body) {
  const r = await fetch(GH + path, {
    method,
    headers: {
      Authorization: "Bearer " + env.GITHUB_TOKEN,
      Accept: "application/vnd.github+json",
      "User-Agent": "spine-broker",
      "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    let msg = "HTTP " + r.status;
    try { msg = (await r.json()).message || msg; } catch (_) { /* ignore */ }
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

function bufToBase64(buf) {
  const bytes = new Uint8Array(buf);
  const chunks = [];
  const STEP = 3 * 16384;
  for (let i = 0; i < bytes.length; i += STEP) {
    let bin = "";
    const end = Math.min(i + STEP, bytes.length);
    for (let k = i; k < end; k++) bin += String.fromCharCode(bytes[k]);
    chunks.push(btoa(bin));
  }
  return chunks.join("");
}

function b64Encode(str) {
  return btoa(String(str));
}

function b64ToBytes(b64) {
  const bin = atob(b64);
  const len = bin.length;
  const out = new Uint8Array(len);
  for (let i = 0; i < len; i++) out[i] = bin.charCodeAt(i);
  return out;
}

async function resultsBranch(env) {
  return j(env, "GET", `/repos/${REPO}/branches/${RESULTS}`);
}

async function resultsTree(env) {
  const b = await resultsBranch(env);
  return { branch: b, tree: await j(env, "GET", `/repos/${REPO}/git/trees/${b.commit.sha}?recursive=1`) };
}

function safeName(job) {
  return String(job || "").replace(/[^A-Za-z0-9._-]/g, "_");
}

async function historyItems(env) {
  let res;
  try {
    res = await resultsTree(env);
  } catch (_) {
    return [];
  }
  const index = (res.tree.tree || []).find((t) => t.type === "blob" && t.path === "history/index.json");
  if (index) {
    try {
      const blob = await j(env, "GET", `/repos/${REPO}/git/blobs/${index.sha}`);
      const data = JSON.parse(atob(blob.content.replace(/\n/g, "")));
      if (data && Array.isArray(data.items)) return data.items;
    } catch (_) { /* fall back to tree scan */ }
  }
  return (res.tree.tree || [])
    .filter((t) => t.type === "blob" && /^history\/.+\.zip$/.test(t.path))
    .map((t) => ({
      job: t.path.replace(/^history\//, "").replace(/\.zip$/, ""),
      file: t.path,
      bytes: t.size || 0,
      date: ((res.branch.commit.commit.committer || {}).date) || "",
      spine: false,
    }))
    .sort((a, b) => (b.date || "").localeCompare(a.date || ""));
}

async function handleHistory(request, env) {
  return json({ items: await historyItems(env) });
}

const FETCH_RE = /^https?:\/\/[\w.-]+\.[a-z]{2,}(\S*)$/i;

const PRIVATE_HOST = /^(localhost$|127\.|10\.|192\.168\.|169\.254\.|0\.0\.0\.0$|\[?::1\]?$|172\.(1[6-9]|2\d|3[01])\.)/i;

// ---------------------------------------------------------------------------
// Защита /proxy от абьюза: лимит запросов на IP + необязательный общий секрет.
//
// Durable Object в проекте НЕ используется (в worker/wrangler.toml только
// KV-биндинг VISITS), поэтому счётчики живут в памяти изолята воркера.
// Следствия, которые надо понимать:
//   * лимит приблизительный — на нескольких изолятах он действует как N
//     независимых счётчиков, при холодном старте счётчики обнуляются;
//   * при переезде на Durable Object достаточно вынести bucketFor/hit в класс
//     с одним ключом на IP — контракт эндпоинта не изменится;
//   * Map ограничен MAX_BUCKETS и чистится по sweep (см. ниже), чтобы не расти
//     в памяти при сканировании с тысяч разных IP.
// Этого достаточно, чтобы отсечь массовый абьюз одного источника, но НЕ
// защищает от распределённого флуда — для этого Cloudflare Rate Limiting.
// В CI каждый прогон приходит со свежего IP, поэтому умолчания щедрые.
// ---------------------------------------------------------------------------
const RATE_LIMIT = 600;         // запросов /proxy на IP за окно
const TOKEN_RATE_LIMIT = 6000;  // запросов /proxy на токен за окно (CI: IP у раннеров общие)
const RATE_WINDOW_MS = 600000;  // 10 минут
const AUTH_FAIL_LIMIT = 10;     // неудачных X-Spine-Token на IP за окно
const MAX_BUCKETS = 20000;      // больше IP одновременно не держим
const SWEEP_MS = 60000;         // как часто чистим протухшие бакеты
const PROXY_MAX_BYTES = 8 * 1024 * 1024;

const buckets = new Map();      // ip -> { hits: number[], fails: number[] }
let lastSweep = 0;

function numEnv(env, name, dflt) {
  const v = Number(env[name]);
  return Number.isFinite(v) && v > 0 ? Math.floor(v) : dflt;
}

function rateCfg(env) {
  return {
    limit: numEnv(env, "SPINE_PROXY_RATE_LIMIT", RATE_LIMIT),
    tokenLimit: numEnv(env, "SPINE_PROXY_TOKEN_RATE_LIMIT", TOKEN_RATE_LIMIT),
    windowMs: numEnv(env, "SPINE_PROXY_RATE_WINDOW_MS", RATE_WINDOW_MS),
    failLimit: numEnv(env, "SPINE_PROXY_AUTH_LIMIT", AUTH_FAIL_LIMIT),
  };
}

// скользящее окно по массиву таймстемпов: список ограничен лимитом,
// поэтому память на один IP константная.
function hitWindow(list, limit, windowMs, now) {
  const cutoff = now - windowMs;
  let drop = 0;
  while (drop < list.length && list[drop] <= cutoff) drop++;
  if (drop) list.splice(0, drop);
  if (list.length >= limit) {
    return {
      ok: false,
      remaining: 0,
      retryAfter: Math.max(1, Math.ceil((list[0] + windowMs - now) / 1000)),
      reset: list[0] + windowMs,
    };
  }
  list.push(now);
  return { ok: true, remaining: limit - list.length, reset: now + windowMs };
}

function sweepBuckets(now, windowMs) {
  if (now - lastSweep < SWEEP_MS) return;
  lastSweep = now;
  for (const [ip, b] of buckets) {
    const win = now - windowMs;
    while (b.hits.length && b.hits[0] <= win) b.hits.shift();
    while (b.fails.length && b.fails[0] <= win) b.fails.shift();
    if (!b.hits.length && !b.fails.length) buckets.delete(ip);
  }
  while (buckets.size > MAX_BUCKETS) {          // вытесняем самые старые (LRU)
    const oldest = buckets.keys().next().value;
    if (oldest === undefined) break;
    buckets.delete(oldest);
  }
}

function bucketFor(ip) {
  let b = buckets.get(ip);
  if (!b) {
    b = { hits: [], fails: [] };
    buckets.set(ip, b);
  } else {
    buckets.delete(ip);                        // обновляем позицию для LRU
    buckets.set(ip, b);
  }
  return b;
}

function clientIp(request) {
  const ip = (request.headers.get("CF-Connecting-IP") || "").trim();
  if (ip) return ip;
  const xff = (request.headers.get("X-Forwarded-For") || "").split(",")[0].trim();
  return xff || "anon";                        // локальная отладка без CF
}

function rateHeaders(h) {
  return {
    "X-RateLimit-Limit": String(h.limit),
    "X-RateLimit-Remaining": String(h.remaining),
    "X-RateLimit-Reset": String(Math.ceil(h.reset / 1000)),
  };
}

function tooMany(retryAfter, h, scope) {
  return json({ error: "rate limit exceeded (" + scope + ")", retry_after: retryAfter }, 429, {
    "Retry-After": String(retryAfter),
    ...rateHeaders(h),
  });
}

function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function proxySecret(env) {
  return String(env.SPINE_PROXY_TOKEN || "").trim();
}

function givenToken(request) {
  return String(request.headers.get("X-Spine-Token") || "").trim();
}

function tokenMatches(request, env) {
  const secret = proxySecret(env);
  return !!secret && safeEqual(givenToken(request), secret);
}

// Ключ бакета для запросов с токеном: сам токен в памяти не держим,
// только короткий необратимый хеш.
function tokenKey(request) {
  const t = givenToken(request);
  let h = 0x811c9dc5;
  for (let i = 0; i < t.length; i++) {
    h ^= t.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16);
}

// Секрет задан -> /proxy закрыт; не задан -> работает открыто (текущее поведение).
function checkToken(request, env, bucket, c, now) {
  const secret = proxySecret(env);
  if (!secret) return null;
  if (safeEqual(givenToken(request), secret)) return null;
  const f = hitWindow(bucket.fails, c.failLimit, c.windowMs, now);
  if (!f.ok) return tooMany(f.retryAfter, { limit: c.failLimit, remaining: 0, reset: f.reset }, "auth");
  return json({ error: "proxy token required" }, 401, {
    "WWW-Authenticate": 'Bearer realm="spine-proxy", header="X-Spine-Token"',
    ...rateHeaders({ limit: c.failLimit, remaining: Math.max(0, c.failLimit - bucket.fails.length), reset: now + c.windowMs }),
  });
}

const HOST_RE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*(?::\d{1,5})?$/;
const HOST6_RE = /^\[[0-9a-f:.]+\](?::\d{1,5})?$/;

// Апстрим строится только из ?url=, Host в него не попадает, но инъекция
// (дубль Host, CRLF, чужой домен) ломает логи/кэш/счётчики -> 421.
// Строгий allowlist включается через SPINE_PROXY_HOSTS (через запятую).
function hostProblem(request, env, reqUrl) {
  const raw = request.headers.get("host");
  if (raw === null) return "Host header required";
  const v = raw.trim().toLowerCase();
  if (!v || v.length > 255) return "empty Host header";
  if (/[\s,;\\/@?#]/.test(v)) return "malformed Host header";
  if (!HOST_RE.test(v) && !HOST6_RE.test(v)) return "malformed Host header";
  const hostname = v.startsWith("[")
    ? v.slice(0, v.indexOf("]") + 1)
    : v.replace(/:\d{1,5}$/, "");
  const allow = String(env.SPINE_PROXY_HOSTS || "")
    .split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
  if (allow.length) {
    return allow.includes(hostname) ? null : "Host not allowed: " + hostname;
  }
  if (reqUrl.hostname && hostname !== reqUrl.hostname) {
    return "Host does not match request URL";
  }
  return null;
}

async function handleProxy(request, env) {
  const reqUrl = new URL(request.url);

  const hostProblemMsg = hostProblem(request, env, reqUrl);
  if (hostProblemMsg) return json({ error: hostProblemMsg }, 421);

  const c = rateCfg(env);
  const now = Date.now();
  sweepBuckets(now, c.windowMs);
  // С токеном считаем запросы по токену, а не по IP: у GitHub-раннеров IP
  // общие на весь мир, по IP они всегда упираются в лимит и получают 429
  // вместо файла. Без токена всё как раньше — лимит по IP.
  const authed = tokenMatches(request, env);
  const bucket = bucketFor(authed ? "tok:" + tokenKey(request) : "ip:" + clientIp(request));
  const limit = authed ? c.tokenLimit : c.limit;
  const rl = hitWindow(bucket.hits, limit, c.windowMs, now);
  if (!rl.ok) return tooMany(rl.retryAfter, { limit, remaining: 0, reset: rl.reset }, limit + " req/" + Math.round(c.windowMs / 1000) + "s " + (authed ? "per token" : "per IP"));
  const rlHeaders = rateHeaders({ limit, remaining: rl.remaining, reset: rl.reset });

  const denied = checkToken(request, env, bucket, c, now);
  if (denied) return denied;

  const raw = reqUrl.searchParams.get("url");
  if (!raw) return json({ error: "url required" }, 400, rlHeaders);
  let target;
  try {
    target = new URL(raw);
  } catch (_) {
    return json({ error: "bad url" }, 400, rlHeaders);
  }
  if (target.protocol !== "https:") return json({ error: "https only" }, 400, rlHeaders);
  if (PRIVATE_HOST.test(target.hostname)) return json({ error: "private host" }, 400, rlHeaders);

  // страховка от редиректов во внутреннюю сеть
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 45000);
  const attemptOnce = async () => {
    const r = await fetch(target.toString(), {
      redirect: "follow",
      signal: controller.signal,
      headers: {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          + "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": target.origin + "/",
      },
    });
    const len = Number(r.headers.get("content-length") || 0);
    if (len > PROXY_MAX_BYTES) {
      return { error: "too large", bytes: len };
    }
    const buf = await r.arrayBuffer();
    // тот же лимит для ответов без content-length (chunked/сжатые)
    if (buf.byteLength > PROXY_MAX_BYTES) {
      return { error: "too large", bytes: buf.byteLength };
    }
    return { status: r.status,
             type: r.headers.get("content-type") || "application/octet-stream",
             body: buf };
  };

  let resp = null;
  try {
    resp = await attemptOnce();
  } catch (_e) {
    try {                                   // одна повторная попытка
      resp = await attemptOnce();
    } catch (e) {
      clearTimeout(timer);
      return json({ error: "proxy fetch failed: " + String(e).slice(0, 120) }, 502, rlHeaders);
    }
  }
  clearTimeout(timer);
  if (resp.error) {
    return json({ error: resp.error, bytes: resp.bytes || 0 }, 413, rlHeaders);
  }
  return new Response(resp.body, {
    status: resp.status,
    headers: {
      "Content-Type": resp.type,
      "X-Proxy-Status": String(resp.status),
      "X-Proxy-Url": target.toString(),
      ...rlHeaders,
      ...CORS,
    },
  });
}

async function handleConvert(request, env) {
  const ctype = request.headers.get("content-type") || "";
  let buf = new ArrayBuffer(0);
  let sourceUrl = "";

  if (ctype.includes("application/json")) {
    // режим «ссылка на игру»: CI сам выкачает ассеты по этой ссылке
    let payload;
    try {
      payload = await request.json();
    } catch (_) {
      return json({ error: "bad json" }, 400);
    }
    sourceUrl = String(payload && payload.url ? payload.url : "").trim();
    if (!FETCH_RE.test(sourceUrl)) {
      return json({ error: "нужен http(s)-адрес игры в поле url" }, 400);
    }
  } else {
    try {
      buf = await request.arrayBuffer();
    } catch (_) {
      return json({ error: "cannot read body" }, 400);
    }
    if (buf.byteLength === 0) return json({ error: "empty body" }, 400);
    if (buf.byteLength > MAX_BYTES) {
      return json({ error: "too large: " + buf.byteLength + " bytes (max " + MAX_BYTES + ")" }, 413);
    }
  }

  const job = "convert-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
  const wf = await j(env, "GET", `/repos/${REPO}/contents/.github/workflows/web-convert.yml`);

  let parent = null;
  try {
    parent = (await j(env, "GET", `/repos/${REPO}/git/ref/heads/${INBOX}`)).object.sha;
  } catch (_) { /* ветки нет - создаём */ }

  const entries = [
    { path: ".github/workflows/web-convert.yml", mode: "100644", type: "blob", sha: wf.sha },
  ];
  if (sourceUrl) {
    const urlBlob = await j(env, "POST", `/repos/${REPO}/git/blobs`, {
      content: b64Encode(sourceUrl), encoding: "base64",
    });
    entries.push({ path: "fetch-url.txt", mode: "100644", type: "blob", sha: urlBlob.sha });
  } else {
    const blobRes = await j(env, "POST", `/repos/${REPO}/git/blobs`, {
      content: bufToBase64(buf), encoding: "base64",
    });
    if (typeof blobRes.size === "number" && blobRes.size !== buf.byteLength) {
      return json({ error: "upload truncated: sent " + buf.byteLength + ", stored " + blobRes.size }, 500);
    }
    entries.push({ path: "input.zip", mode: "100644", type: "blob", sha: blobRes.sha });
  }

  const tree = await j(env, "POST", `/repos/${REPO}/git/trees`, { tree: entries });
  const commit = await j(env, "POST", `/repos/${REPO}/git/commits`, {
    message: job,
    tree: tree.sha,
    parents: parent ? [parent] : [],
  });
  if (parent) {
    await j(env, "PATCH", `/repos/${REPO}/git/refs/heads/${INBOX}`, { sha: commit.sha, force: true });
  } else {
    await j(env, "POST", `/repos/${REPO}/git/refs`, { ref: `refs/heads/${INBOX}`, sha: commit.sha });
  }

  return json({ job, mode: sourceUrl ? "url" : "upload" });
}

async function handleStatus(request, env) {
  const job = new URL(request.url).searchParams.get("job");
  if (!job) return json({ error: "job required" }, 400);
  let b;
  try {
    b = await resultsBranch(env);
  } catch (_) {
    return json({ ready: false });
  }
  return json({ ready: (b.commit.commit.message || "").includes(job) });
}

async function handleDownload(request, env) {
  const url = new URL(request.url);
  const archive = url.searchParams.get("archive");
  const job = url.searchParams.get("job");
  let tree, headMessage, headSha = "";
  try {
    const res = await resultsTree(env);
    tree = res.tree;
    headMessage = res.branch.commit.commit.message || "";
    headSha = res.branch.commit.sha || "";
  } catch (_) {
    return json({ ready: false }, 202);
  }

  let entry, name;
  if (archive) {
    const safe = safeName(archive);
    entry = (tree.tree || []).find((t) => t.type === "blob" && t.path === `history/${safe}.zip`);
    name = `spine-${safe}.zip`;
    if (!entry) return json({ error: "archive not found: " + archive }, 404);
  } else {
    if (!job) return json({ error: "job required" }, 400);
    if (!headMessage.includes(job)) return json({ ready: false }, 202);
    // сначала неизменяемый архив конкретной задачи, иначе общий output.zip
    const safe = safeName(job);
    const own = (tree.tree || []).find((t) => t.type === "blob" && t.path === `history/${safe}.zip`);
    entry = own || (tree.tree || []).find((t) => t.type === "blob" && t.path === "output.zip");
    name = own ? `spine-${safe}.zip` : "spine-converted.zip";
    if (!entry) return json({ error: "output.zip not found in results" }, 500);
  }

  // большие архивы отдаём редиректом на raw.githubusercontent: держать
  // сотни мегабайт в памяти воркера нельзя (Cloudflare 1102)
  if (entry.size && entry.size > 6 * 1024 * 1024 && headSha) {
    const raw = `https://raw.githubusercontent.com/${REPO}/${headSha}/${entry.path}`;
    return new Response(null, {
      status: 302,
      headers: {
        Location: raw,
        "Content-Disposition": 'attachment; filename="' + name + '"',
        ...CORS,
      },
    });
  }

  const blob = await j(env, "GET", `/repos/${REPO}/git/blobs/${entry.sha}`);
  const bytes = b64ToBytes(blob.content);
  return new Response(bytes, {
    status: 200,
    headers: {
      "Content-Type": "application/zip",
      ...CORS,
      "Content-Disposition": 'attachment; filename="' + name + '"',
    },
  });
}

async function handleVisit(request, env) {
  if (!env.VISITS) return json({ visits: null, tracked: false });
  const total = await env.VISITS.get("visits", { type: "json" }).catch(() => null);
  const visits = (total && Number(total.value)) || 0;
  const next = visits + 1;
  await env.VISITS.put("visits", JSON.stringify({ value: next, updated: new Date().toISOString() }));
  return json({ visits: next, tracked: true });
}

async function handleStats(request, env) {
  let visits = null;
  if (env.VISITS) {
    try {
      const v = await env.VISITS.get("visits", { type: "json" });
      visits = v ? Number(v.value) || 0 : 0;
    } catch (_) { /* ignore */ }
  }
  const items = await historyItems(env);
  const totalBytes = items.reduce((a, b) => a + (Number(b.bytes) || 0), 0);
  return json({
    visits,
    conversions: items.length,
    totalBytes,
    last: items.length ? items[0].date || "" : "",
  });
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }
    if (!env.GITHUB_TOKEN) return json({ error: "broker not configured" }, 500);
    const url = new URL(request.url);
    try {
      if (url.pathname === "/") return json({ ok: true });
      if (request.method === "POST" && url.pathname === "/convert") {
        return await handleConvert(request, env);
      }
      if (request.method === "GET" && url.pathname === "/proxy") {
        return handleProxy(request, env);
      }
      if (request.method === "GET" && url.pathname === "/status") {
        return await handleStatus(request, env);
      }
      if (request.method === "GET" && url.pathname === "/download") {
        return await handleDownload(request, env);
      }
      if (request.method === "GET" && url.pathname === "/history") {
        return await handleHistory(request, env);
      }
      if (request.method === "GET" && url.pathname === "/visit") {
        return await handleVisit(request, env);
      }
      if (request.method === "GET" && url.pathname === "/stats") {
        return await handleStats(request, env);
      }
      return json({ error: "not found" }, 404);
    } catch (e) {
      return json({ error: String(e.message || e) }, 500);
    }
  },
};