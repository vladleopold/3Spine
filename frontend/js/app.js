(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const els = {
    srv: $("srv"),
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
    histRefresh: $("hist-refresh"),
    histList: $("history-list"),
  };

  const BROKER = "https://spine-broker.leopolds2010.workers.dev";
  const MAX_ZIP = 35 * 1024 * 1024;

  let files = [];
  let lastZip = null;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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
    els.fileSummary.classList.toggle("hidden", files.length === 0);
    els.fileCount.textContent = files.length + " (скелетов: " + skels.length + ")";
    els.start.disabled = files.length === 0 || skels.length === 0;
  }

  async function checkServer() {
    try {
      const r = await fetch(BROKER + "/", { method: "GET" });
      if (r.ok) {
        els.srv.textContent = "сервер: онлайн";
        els.srv.className = "health ok";
      } else {
        els.srv.textContent = "сервер: не отвечает";
        els.srv.className = "health bad";
      }
    } catch (_) {
      els.srv.textContent = "сервер: недоступен";
      els.srv.className = "health bad";
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
      if (!item) return;
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

  /* ---------------- zip ---------------- */

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

  /* ---------------- convert via broker -> Actions ---------------- */

  async function startConvert(blob) {
    log("> Отправляем на сервер (" + (blob.size / 1048576).toFixed(1) + " МБ)…", "dim");
    const r = await fetch(BROKER + "/convert", {
      method: "POST",
      body: blob,
      headers: { "Content-Type": "application/octet-stream" },
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || ("сервер: HTTP " + r.status));
    return data.job;
  }

  async function waitStatus(job, t0) {
    const deadline = t0 + 5 * 60 * 1000;
    let lastLog = 0;
    while (Date.now() < deadline) {
      const r = await fetch(BROKER + "/status?job=" + encodeURIComponent(job), { method: "GET" });
      const d = await r.json().catch(() => ({}));
      if (d.ready) return;
      if (Date.now() - lastLog > 7000) { log("… конвертация в GitHub Actions…", "dim"); lastLog = Date.now(); }
      await sleep(4000);
    }
    throw new Error("Превышено время ожидания (5 минут)");
  }

  async function fetchResult(job) {
    const r = await fetch(BROKER + "/download?job=" + encodeURIComponent(job), { method: "GET" });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d.error || ("HTTP " + r.status));
    }
    return r.blob();
  }

  async function extractLog() {
    const z = await JSZip.loadAsync(lastZip);
    const f = z.file("convert-log.txt");
    return f ? f.async("string") : "";
  }

  /* ---------------- history ---------------- */

  function fmtBytes(n) {
    if (!n && n !== 0) return "";
    if (n < 1024) return n + " Б";
    if (n < 1048576) return (n / 1024).toFixed(1) + " КБ";
    return (n / 1048576).toFixed(2) + " МБ";
  }

  function fmtDate(s) {
    if (!s) return "";
    const d = new Date(s);
    if (isNaN(d.getTime())) return s;
    const p = (x) => String(x).padStart(2, "0");
    return d.toLocaleString("ru-RU", {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit",
    }) + (d.getHours() === undefined ? "" : ":" + p(d.getSeconds()));
  }

  function renderHistory(items) {
    els.histList.innerHTML = "";
    if (!items.length) {
      const d = document.createElement("div");
      d.className = "history-empty";
      d.textContent = "история пока пуста — архивы появятся после первой конвертации";
      els.histList.appendChild(d);
      return;
    }
    for (const it of items) {
      const row = document.createElement("div");
      row.className = "history-item";

      const job = document.createElement("div");
      job.className = "h-job";
      job.textContent = it.job;
      job.title = it.file || it.job;

      const meta = document.createElement("div");
      meta.className = "h-meta";
      const bits = [fmtDate(it.date), fmtBytes(it.bytes)].filter(Boolean);
      meta.textContent = bits.join(" · ");

      row.appendChild(job);
      if (it.spine) {
        const tag = document.createElement("span");
        tag.className = "h-tag";
        tag.textContent = ".SPINE";
        row.appendChild(tag);
      }
      row.appendChild(meta);

      const a = document.createElement("a");
      a.className = "btn ghost small";
      a.href = BROKER + "/download?archive=" + encodeURIComponent(it.job);
      a.textContent = "Скачать";
      a.setAttribute("download", "");
      row.appendChild(a);

      els.histList.appendChild(row);
    }
  }

  async function loadHistory() {
    try {
      const r = await fetch(BROKER + "/history", { method: "GET" });
      const d = await r.json().catch(() => ({}));
      renderHistory(Array.isArray(d.items) ? d.items : []);
    } catch (_) {
      els.histList.innerHTML = "";
      const d = document.createElement("div");
      d.className = "history-empty";
      d.textContent = "не удалось загрузить историю";
      els.histList.appendChild(d);
    }
  }

  els.histRefresh.addEventListener("click", loadHistory);

  /* ---------------- start / download ---------------- */

  els.start.addEventListener("click", async () => {
    els.start.disabled = true;
    els.download.classList.add("hidden");
    lastZip = null;
    els.log.textContent = "";
    setStatus("пакуем…", "run");
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
    try {
      const t0 = Date.now();
      log("> Пакуем и отправляем…", "dim");
      const job = await startConvert(blob);
      log("> Задача: " + job, "dim");
      setStatus("конвертация…", "run");
      await waitStatus(job, t0);
      log("> Результат готов, скачиваем…", "dim");
      lastZip = await fetchResult(job);
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
      loadHistory();
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
  checkServer();
  renderFileList();
  loadHistory();
})();
