(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const els = {
    backendUrl: $("backend-url"),
    saveUrl: $("save-url"),
    health: $("health"),
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

  let files = [];
  let lastToken = null;

  const LS_KEY = "spine-backend-url";
  const DEFAULT_URL = "http://127.0.0.1:8080";

  /* ---------------- helpers ---------------- */

  function backendUrl() {
    const v = (els.backendUrl.value || "").trim().replace(/\/+$/, "");
    return v || DEFAULT_URL;
  }

  function logLink(text, cls) {
    const span = document.createElement("span");
    if (cls) span.className = cls;
    span.textContent = text;
    els.log.appendChild(span);
  }

  function log(text, cls) {
    logLink(text, cls);
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

  /* ---------------- health ---------------- */

  async function checkHealth() {
    try {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 4000);
      const r = await fetch(backendUrl() + "/health", { signal: ctl.signal });
      clearTimeout(t);
      if (!r.ok) throw new Error("HTTP " + r.status);
      const j = await r.json();
      els.health.textContent = "✓ " + (j.converter ? "сервер онлайн" : "сервер онлайн");
      els.health.className = "health ok";
    } catch (e) {
      els.health.textContent = "✗ недоступен";
      els.health.className = "health bad";
    }
  }

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
    // dedupe
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
    lastToken = null;
    els.folderInput.value = "";
    els.log.textContent = "";
    els.download.classList.add("hidden");
    renderFileList();
    setStatus("готов", "");
  });

  /* ---------------- backend url ---------------- */

  els.backendUrl.value = localStorage.getItem(LS_KEY) || DEFAULT_URL;
  els.saveUrl.addEventListener("click", () => {
    localStorage.setItem(LS_KEY, backendUrl());
    checkHealth();
  });
  els.backendUrl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { localStorage.setItem(LS_KEY, backendUrl()); checkHealth(); }
  });

  /* ---------------- start ---------------- */

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

  els.start.addEventListener("click", async () => {
    els.start.disabled = true;
    els.download.classList.add("hidden");
    lastToken = null;
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
    log("> ZIP собран (" + (blob.size / 1024 / 1024).toFixed(1) + " МБ), отправляем на сервер…", "dim");
    try {
      const r = await fetch(backendUrl() + "/api/convert", {
        method: "POST",
        headers: { "Content-Type": "application/zip" },
        body: blob,
      });
      if (!r.ok) {
        let msg = "HTTP " + r.status;
        try { msg = (await r.json()).error || msg; } catch (_) { /* ignore */ }
        throw new Error(msg);
      }
      const j = await r.json();
      for (const line of j.logs || []) {
        const cls = /FAIL|\bошибка\b/i.test(line) ? "err"
                   : /^OK|успешно|скопировано/i.test(line) ? "ok"
                   : /^\S|→/.test(line) ? "info" : "dim";
        log(String(line), cls);
      }
      lastToken = j.token;
      if (j.ok > 0) {
        log("==== Готово: " + (j.ok || 0) + " OK, " + (j.failed || 0) + " failed ====", "ok");
        els.download.classList.remove("hidden");
        setStatus("готово", "ok");
      } else {
        setStatus("ошибок больше, чем результатов", "err");
      }
    } catch (e) {
      log("Ошибка сервера: " + e.message, "err");
      log("Проверьте адрес сервера (⚙ салфетка справа вверху) и что Linux-раннер запущен.", "dim");
      setStatus("ошибка", "err");
    } finally {
      els.start.disabled = files.length === 0;
    }
  });

  els.download.addEventListener("click", () => {
    if (!lastToken) return;
    const a = document.createElement("a");
    a.href = backendUrl() + "/results/" + lastToken + ".zip";
    a.download = "spine-converted.zip";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 500);
  });

  /* init */
  checkHealth();
  setInterval(checkHealth, 15000);
})();