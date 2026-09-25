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
  "Access-Control-Allow-Headers": "content-type, authorization",
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
  const STEP = 0x8000;
  for (let i = 0; i < bytes.length; i += STEP) {
    let bin = "";
    const end = Math.min(i + STEP, bytes.length);
    for (let k = i; k < end; k++) bin += String.fromCharCode(bytes[k]);
    chunks.push(btoa(bin));
  }
  return chunks.join("");
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

async function handleHistory(request, env) {
  let res;
  try {
    res = await resultsTree(env);
  } catch (_) {
    return json({ items: [] });
  }
  const index = (res.tree.tree || []).find((t) => t.type === "blob" && t.path === "history/index.json");
  if (index) {
    try {
      const blob = await j(env, "GET", `/repos/${REPO}/git/blobs/${index.sha}`);
      const data = JSON.parse(atob(blob.content.replace(/\n/g, "")));
      if (data && Array.isArray(data.items)) return json({ items: data.items });
    } catch (_) { /* fall back to tree scan */ }
  }
  const items = (res.tree.tree || [])
    .filter((t) => t.type === "blob" && /^history\/.+\.zip$/.test(t.path))
    .map((t) => ({
      job: t.path.replace(/^history\//, "").replace(/\.zip$/, ""),
      file: t.path,
      bytes: t.size || 0,
      date: (res.branch.commit.commit.committer && res.branch.commit.commit.committer.date) || "",
      spine: false,
    }))
    .sort((a, b) => (b.date || "").localeCompare(a.date || ""));
  return json({ items });
}

async function handleConvert(request, env) {
  let buf;
  try {
    buf = await request.arrayBuffer();
  } catch (_) {
    return json({ error: "cannot read body" }, 400);
  }
  if (buf.byteLength === 0) return json({ error: "empty body" }, 400);
  if (buf.byteLength > MAX_BYTES) {
    return json({ error: "too large: " + buf.byteLength + " bytes (max " + MAX_BYTES + ")" }, 413);
  }
  const job = "convert-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);

  const blobRes = await j(env, "POST", `/repos/${REPO}/git/blobs`, {
    content: bufToBase64(buf), encoding: "base64",
  });
  const wf = await j(env, "GET", `/repos/${REPO}/contents/.github/workflows/web-convert.yml`);

  let parent = null;
  try {
    parent = (await j(env, "GET", `/repos/${REPO}/git/ref/heads/${INBOX}`)).object.sha;
  } catch (_) { /* ветки нет - создаём */ }

  const tree = await j(env, "POST", `/repos/${REPO}/git/trees`, {
    tree: [
      { path: ".github/workflows/web-convert.yml", mode: "100644", type: "blob", sha: wf.sha },
      { path: "input.zip", mode: "100644", type: "blob", sha: blobRes.sha },
    ],
  });
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

  return json({ job });
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
  let tree, headMessage;
  try {
    const res = await resultsTree(env);
    tree = res.tree;
    headMessage = res.branch.commit.commit.message || "";
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
    entry = (tree.tree || []).find((t) => t.type === "blob" && t.path === "output.zip");
    name = "spine-converted.zip";
    if (!entry) return json({ error: "output.zip not found in results" }, 500);
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
      if (request.method === "GET" && url.pathname === "/status") {
        return await handleStatus(request, env);
      }
      if (request.method === "GET" && url.pathname === "/download") {
        return await handleDownload(request, env);
      }
      if (request.method === "GET" && url.pathname === "/history") {
        return await handleHistory(request, env);
      }
      return json({ error: "not found" }, 404);
    } catch (e) {
      return json({ error: String(e.message || e) }, 500);
    }
  },
};