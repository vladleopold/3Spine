(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const els = {
    ghStatus: $("gh-status"),
    ghLogin: $("gh-login"),
    ghModal: $("gh-modal"),
    ghToken: $("gh-token"),
    ghRemember: $("gh-remember"),
    ghSave: $("gh-save"),
    ghCancel: $("gh-cancel"),
    ghErr: $("gh-err"),
    dropzone: $("dropzone"),
    pickFolder: $("pick-folder"),
    folderInput: $("folder-input"),
    fileSummary: $("file-summary"),
    fileCount: $("file-count"),
    fileList: $("file-list"),
    start: $("start"),
    download: $("download"),
    clear: $("clear"),
    log: $("log"),
    status: $("status"),
  };

  const GH_REPO = "vladleopold/3Spine";
  const BR_INBOX = "inbox";
  const BR_RESULTS = "results";
  const RESULT_FILE = "output.zip";
  const TOKEN_KEY = "spine-gh";
  const TOKEN_KEY_LS = "spine-gh-persist";
  const MAX_ZIP = 45 * 1024 * 1024;

  let files = [];
  let ghToken = null;
  let ghUser = null;
  let lastZip = null;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  /* ---------------- GitHub API ---------------- */

  async function gh(method, path, body) {
    const r = await fetch("https://api.github.com" + path, {
      method,
      headers: {
        Authorization: "Bearer " + ghToken,
        Accept: "application/vnd.github+json",
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

  async function connectGH(token) {
    const prev = ghToken;
    ghToken = token;
    try {
      ghUser = await gh("GET", "/user");
      return ghUser;
    } catch (e) {
      ghToken = prev;
      ghUser = null;
      throw e;
    }
  }

  function setGHStatus() {
    if (ghUser) {
      els.ghStatus.textContent = "GitHub: @" + ghUser.login;
      els.ghStatus.className = "health ok";
      els.ghLogin.textContent = "Сменить токен";
    } else {
      els.ghStatus.textContent = "GitHub: не подключён";
      els.ghStatus.className = "health bad";
      els.ghLogin.textContent = "Подключить GitHub";
    }
  }

  function saveToken() {
    sessionStorage.setItem(TOKEN_KEY, ghToken);
    if (els.ghRemember.checked) localStorage.setItem(TOKEN_KEY_LS, ghToken);
    else localStorage.removeItem(TOKEN_KEY_LS);
  }

  async function initGH() {
    const token = sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY_LS);
    if (!token) { setGHStatus(); return; }
    try {
      await connectGH(token);
    } catch (_) {
      sessionStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(TOKEN_KEY_LS);
    }
    setGHStatus();
  }

  /* ---------------- UI helpers ---------------- */

  function log(text, cls) {
    const span = document.createElement("span");
    if (cls) span.className = cls;
    span.textContent = text;
    els.log.appendChild(span);
    els.log.append("\n");
    els.log.scrollTop = els.log.scrollHeight;
  }

  function setStatus(text, cls) {
    els.status.textContent = text;
    els.status.className = "status" + (cls ? " " + cls : "");
  }

  function renderFileList() {
    els.fileCount.textContent = files.length;
    els.fileList.innerHTML = "";
    const skels = files.filter((f) => /\.skel$/i.test(f.name));
    const others = files.filter((f) => !/\.skel$/i.test(f.name));
    const shown = [...skels, ...others.slice(0, 60)];
    for (const f of shown) {
      const d = document.createElement("div");
      d.textContent = (f.webkitRelativePath || f.name);
      if (/\.skel$/i.test(f.name)) d.className = "sk";
      els.fileList.appendChild(d);
    }
    if (others.length > 60) {
      const d = document.createElement("div");
      d.textContent = "... и ещё " + (others.length - 60) + " файлов";
      els.fileList.appendChild(d);
    }
    const skCount = skels.length;
    els.fileSummary.classList.toggle("hidden", files.length === 0);
    els.fileCount.textContent = files.length + " (скелетов: " + skCount + ")";
    els.start.disabled = files.length === 0 || skCount === 0;
  }

  /* ---------------- GitHub auth modal ---------------- */

  function openModal() {
    els.ghErr.classList.add("hidden");
    els.ghToken.value = "";
    els.ghModal.classList.remove("hidden");
    els.ghToken.focus();
  }
  function closeModal() {
    els.ghModal.classList.add("hidden");
  }

  els.ghLogin.addEventListener("click", openModal);
  els.ghCancel.addEventListener("click", closeModal);
  els.ghModal.addEventListener("click", (e) => { if (e.target === els.ghModal) closeModal(); });
  els.ghToken.addEventListener("keydown", (e) => { if (e.key === "Enter") els.ghSave.click(); });

  els.ghSave.addEventListener("click", async () => {
    const token = els.ghToken.value.trim();
    if (!token) { els.ghErr.textContent = "Введите токен"; els.ghErr.classList.remove("hidden"); return; }
    try {
      const u = await connectGH(token);
      saveToken();
      closeModal();
      setGHStatus();
      log("Подключено: @" + u.login, "ok");
    } catch (e) {
      els.ghErr.textContent = "Токен не принят: " + e.message;
      els.ghErr.classList.remove("hidden");
      setGHStatus();
    }
  });

  /* ---------------- folder pick ---------------- */

  els.pickFolder.addEventListener("click", () => els.folderInput.click());
  els.dropzone.addEventListener("click", () => els.folderInput.click());
  els.dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); els.folderInput.click(); }
  });

  els.dropzone.addEventListener("dragover", (e) => {
    e.preventDefault();
    els.dropzone.classList.add("drag");
  });
  els.dropzone.addEventListener("dragleave", () => els.dropzone.classList.remove("drag"));

  els.dropzone.addEventListener("drop", async (e) => {
    e.preventDefault();
    els.dropzone.classList.remove("drag");
    const items = e.dataTransfer.items;
    const collected = [];
    async function walk(item, path) {
      if (item.isFile) {
        const f = await new Promise((res) => item.file(res));
        if (!path) path = f.name;
        Object.defineProperty(f, "webkitRelativePath", { value: path, configurable: true });
        collected.push(f);
      } else if (item.isDirectory) {
        const reader = item.createReader();
        let batch;
        do {
          batch = await new Promise((res) => reader.readEntries(res));
          for (const en of batch) await walk(en, path ? path + "/" + en.name : en.name);
        } while (batch.length);
      }
    }
    for (const item of items) await walk(item.webkitGetAsEntry ? item.webkitGetAsEntry() : null, "");
    const seen = new Set();
    files = collected.filter((f) => {
      const k = f.webkitRelativePath || f.name;
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    });
    renderFileList();
  });

  els.folderInput.addEventListener("change", () => {
    files = Array.from(els.folderInput.files || []);
    renderFileList();
  });

  els.clear.addEventListener("click", () => {
    files = [];
    lastZip = null;
    els.folderInput.value = "";
    els.log.textContent = "";
    els.download.classList.add("hidden");
    renderFileList();
    setStatus("готов", "");
  });

  /* ---------------- zip / base64 ---------------- */

  async function buildZip() {
    const zip = new JSZip();
    for (const f of files) {
      zip.file(f.webkitRelativePath || f.name, f);
    }
    return zip.generateAsync({
      type: "blob",
      compression: "DEFLATE",
      compressionOptions: { level: 6 },
    });
  }

  function blobToBase64(blob) {
    return new Promise((res, rej) => {
      const fr = new FileReader();
      fr.onload = () => res(String(fr.result).split(",")[1] || "");
      fr.onerror = () => rej(fr.error);
      fr.readAsDataURL(blob);
    });
  }

  function b64ToBytes(b64) {
    const bin = atob(b64);
    const len = bin.length;
    const bytes = new Uint8Array(len);
    for (let i = 0; i < len; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  }

  /* ---------------- convert via GitHub Actions ---------------- */

  async function pushInputZip(blob) {
    log("> Отправляем архив в GitHub (ветка " + BR_INBOX + ")…", "dim");
    const b64 = await blobToBase64(blob);
    const blobRes = await gh("POST", "/repos/" + GH_REPO + "/git/blobs", {
      content: b64, encoding: "base64",
    });
    const wf = await gh("GET", "/repos/" + GH_REPO + "/contents/.github/workflows/web-convert.yml");
    let parent = null;
    try {
      const ref = await gh("GET", "/repos/" + GH_REPO + "/git/ref/heads/" + BR_INBOX);
      parent = ref.object.sha;
    } catch (_) { /* ветки ещё нет */ }
    const tree = await gh("POST", "/repos/" + GH_REPO + "/git/trees", {
      tree: [
        { path: ".github/workflows/web-convert.yml", mode: "100644", type: "blob", sha: wf.sha },
        { path: "input.zip", mode: "100644", type: "blob", sha: blobRes.sha },
      ],
    });
    const commit = await gh("POST", "/repos/" + GH_REPO + "/git/commits", {
      message: "convert " + new Date().toISOString(),
      tree: tree.sha,
      parents: parent ? [parent] : [],
    });
    if (parent) {
      await gh("PATCH", "/repos/" + GH_REPO + "/git/refs/heads/" + BR_INBOX, { sha: commit.sha, force: true });
    } else {
      await gh("POST", "/repos/" + GH_REPO + "/git/refs", { ref: "refs/heads/" + BR_INBOX, sha: commit.sha });
    }
    log("> Загружено, Actions поднимает Linux…", "dim");
  }

  async function waitForResults(t0) {
    const deadline = Date.now() + 5 * 60 * 1000;
    let lastLog = 0;
    while (Date.now() < deadline) {
      try {
        const br = await gh("GET", "/repos/" + GH_REPO + "/branches/" + BR_RESULTS);
        const date = Date.parse(br.commit.commit.committer.date);
        if (!isNaN(date) && date >= t0 - 5000) return br;
      } catch (_) { /* ещё нет результата */ }
      if (Date.now() - lastLog > 7000) { log("… конвертация в Actions…", "dim"); lastLog = Date.now(); }
      await sleep(5000);
    }
    throw new Error("Превышено время ожидания (5 минут)");
  }

  async function fetchResultBlob(sha) {
    const tree = await gh("GET", "/repos/" + GH_REPO + "/git/trees/" + sha + "?recursive=1");
    let entry = null;
    for (const t of tree.tree) {
      if (t.type === "blob" && t.path === RESULT_FILE) { entry = t; break; }
    }
    if (!entry) throw new Error("Результат не найден в ветке " + BR_RESULTS);
    const b = await gh("GET", "/repos/" + GH_REPO + "/git/blobs/" + entry.sha);
    const bytes = b64ToBytes(b.content);
    return new Blob([bytes], { type: "application/zip" });
  }

  async function extractLog() {
    const z = await JSZip.loadAsync(lastZip);
    const f = z.file("convert-log.txt");
    return f ? f.async("string") : "";
  }

  /* ---------------- start / download ---------------- */

  els.start.addEventListener("click", async () => {
    if (!ghUser) { openModal(); log("Сначала подключите GitHub (токен нужен только для запуска).", "info"); return; }
    els.start.disabled = true;
    els.download.classList.add("hidden");
    lastZip = null;
    els.log.textContent = "";
    setStatus("конвертация…", "run");
    log("> Пакуем файлы…", "dim");
    let blob;
    try {
      blob = await buildZip();
    } catch (e) {
      log("Ошибка упаковки: " + e, "err");
      setStatus("ошибка", "err");
      els.start.disabled = false;
      return;
    }
    if (blob.size > MAX_ZIP) {
      log("Архив слишком большой: " + (blob.size / 1048576).toFixed(1) + " МБ (лимит " + (MAX_ZIP / 1048576) + " МБ).", "err");
      setStatus("ошибка", "err");
      els.start.disabled = false;
      return;
    }
    log("> ZIP собран (" + (blob.size / 1048576).toFixed(1) + " МБ), отправляем в GitHub…", "dim");
    const t0 = Date.now();
    try {
      await pushInputZip(blob);
      const br = await waitForResults(t0);
      log("> Результат готов, скачиваем…", "dim");
      lastZip = await fetchResultBlob(br.commit.sha);
      const logText = await extractLog();
      if (logText) {
        for (const line of logText.split("\n")) {
          if (!line.trim()) continue;
          const cls = /^FAIL/.test(line) ? "err" : /^OK/.test(line) ? "ok" : /^done/.test(line) ? "info" : "dim";
          log(line, cls);
        }
      }
      setStatus("готово", "ok");
      els.download.classList.remove("hidden");
    } catch (e) {
      log("Ошибка: " + e.message, "err");
      setStatus("ошибка", "err");
    } finally {
      els.start.disabled = false;
    }
  });

  els.download.addEventListener("click", () => {
    if (!lastZip) return;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(lastZip);
    a.download = "spine-converted.zip";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  });

  /* init */
  initGH();
  renderFileList();
})();
