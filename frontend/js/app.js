(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const els = {
    srv: $("srv"),
    dropzone: $("dropzone"),
    pickFolder: $("pick-folder"),
    pickZip: $("pick-zip"),
    folderInput: $("folder-input"),
    zipInput: $("zip-input"),
    fileSummary: $("file-summary"),
    fileCount: $("file-count"),
    fileList: $("file-list"),
    srcUrl: $("src-url"),
    urlClear: $("url-clear"),
    start: $("start"),
    download: $("download"),
    clear: $("clear"),
    log: $("log"),
    status: $("status"),
    histRefresh: $("hist-refresh"),
    histList: $("history-list"),
    statsLine: $("stats-line"),
  };

  const BROKER = "https://spine-broker.leopolds2010.workers.dev";
  const MAX_ZIP = 35 * 1024 * 1024;
  const MAX_UNPACKED = 400 * 1024 * 1024;
  const MAX_FILES = 5000;

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

  const INPUT_RE = /\.(skel|json|txt|atlas|xml|png|jpe?g|gif|webp|avif)$/i;
  const SOURCE_RE = /\.(skel|json|txt|spine)$/i;

  function renderFileList() {
    els.fileList.innerHTML = "";
    const skels = files.filter((f) => SOURCE_RE.test(f.name));
    const others = files.filter((f) => !SOURCE_RE.test(f.name));
    const shown = [...skels, ...others.slice(0, 60)];
    for (const f of shown) {
      const d = document.createElement("div");
      d.textContent = (f.webkitRelativePath || f.name);
      if (SOURCE_RE.test(f.name)) d.className = "sk";
      els.fileList.appendChild(d);
    }
    if (others.length > 60) {
      const d = document.createElement("div");
      d.textContent = "... и ещё " + (others.length - 60) + " файлов";
      els.fileList.appendChild(d);
    }
    els.fileSummary.classList.toggle("hidden", files.length === 0);
    els.fileCount.textContent = files.length + " (источников: " + skels.length + ")";
    syncStart();
  }

  // кнопка «Старт» активна, если выбраны файлы ИЛИ введена ссылка на игру
  function syncStart() {
    const hasUrl = !!(els.srcUrl && els.srcUrl.value.trim());
    const supported = files.filter((f) => INPUT_RE.test(f.name));
    els.start.disabled = !(hasUrl || supported.length > 0);
  }

  async function checkServer() {
    try {
      const r = await fetch(BROKER + "/", { method: "GET" });
      if (r.ok) {
        els.srv.title = "Сервер конвертации работает";
        els.srv.className = "health ok";
      } else {
        els.srv.title = "Сервер конвертации не отвечает (HTTP " + r.status + ")";
        els.srv.className = "health bad";
      }
    } catch (_) {
      els.srv.title = "Сервер конвертации недоступен";
      els.srv.className = "health bad";
    }
  }

  /* ---------------- stats ---------------- */

  async function registerVisit() {
    try {
      await fetch(BROKER + "/visit", { method: "GET" });
    } catch (_) { /* ignore */ }
  }

  async function loadStats() {
    let visits = null;
    let conversions = null;
    let totalBytes = 0;
    try {
      const r = await fetch(BROKER + "/stats", { method: "GET" });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || "HTTP " + r.status);
      visits = d.visits === undefined ? null : d.visits;
      conversions = d.conversions || 0;
      totalBytes = d.totalBytes || 0;
    } catch (_) {
      try {
        const r = await fetch(BROKER + "/history", { method: "GET" });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error("no history");
        const items = Array.isArray(d.items) ? d.items : [];
        conversions = items.length;
        totalBytes = items.reduce((a, b) => a + (Number(b.bytes) || 0), 0);
      } catch (__) {
        els.statsLine.textContent = "статистика недоступна";
        return;
      }
    }
    const visitsText = visits === null || visits === undefined ? "—" : visits;
    els.statsLine.innerHTML =
      "посещений: <b>" + visitsText + "</b> · конвертаций: <b>" + conversions + "</b> · архивов: <b>" + fmtBytes(totalBytes) + "</b>";
  }

  /* ---------------- zip input ---------------- */

  function safeRelPath(p) {
    return String(p || "")
      .replace(/\\/g, "/")
      .split("/")
      .filter((seg) => seg && seg !== "." && seg !== "..")
      .join("/");
  }

  async function expandZip(file) {
    const out = [];
    let zip;
    try {
      zip = await JSZip.loadAsync(file);
    } catch (e) {
      throw new Error("не удалось прочитать zip: " + (e && e.message ? e.message : e));
    }
    const names = Object.keys(zip.files).filter((n) => !zip.files[n].dir);
    if (names.length > MAX_FILES) {
      throw new Error("в архиве " + names.length + " файлов — максимум " + MAX_FILES);
    }
    let total = 0;
    const pending = [];
    for (const name of names) {
      const rel = safeRelPath(name);
      if (!rel) continue;
      const entry = zip.files[name];
      pending.push(
        entry.async("blob").then((blob) => {
          total += blob.size;
          if (total > MAX_UNPACKED) throw new Error("распакованный архив больше " + (MAX_UNPACKED / 1048576) + " МБ");
          const base = rel.split("/").pop();
          const nf = new File([blob], base, { type: blob.type, lastModified: Date.now() });
          Object.defineProperty(nf, "webkitRelativePath", { value: rel, configurable: true });
          out.push(nf);
        })
      );
    }
    await Promise.all(pending);
    return out;
  }

  async function useZipFile(file) {
    if (!file) return;
    setStatus("распаковываем zip…", "run");
    log("> Распаковка " + file.name + "…", "dim");
    try {
      const expanded = await expandZip(file);
      files = dedupe(expanded);
      renderFileList();
      log("> Из архива извлечено файлов: " + expanded.length, "dim");
      setStatus("готов", "");
    } catch (e) {
      log("Ошибка: " + e.message, "err");
      setStatus("ошибка", "err");
    }
  }

  function dedupe(list) {
    const seen = new Set();
    return list.filter((f) => {
      const k = f.webkitRelativePath || f.name;
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    });
  }

  /* ---------------- folder pick ---------------- */

  els.pickFolder.addEventListener("click", () => els.folderInput.click());
  els.pickZip.addEventListener("click", () => els.zipInput.click());
  els.dropzone.addEventListener("click", (e) => {
    if (e.target.closest("button")) return;
    els.folderInput.click();
  });
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

    const dt = e.dataTransfer;
    const entries = [];
    const plain = [];
    for (const item of Array.from(dt.items || [])) {
      const entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
      if (entry) entries.push(entry);
      else if (item.kind === "file") {
        const f = item.getAsFile();
        if (f) plain.push(f);
      }
    }
    if (!entries.length) for (const f of Array.from(dt.files || [])) plain.push(f);

    const collected = [];
    const zips = [];

    async function walk(entry, path) {
      if (!entry) return;
      if (entry.isFile) {
        const f = await new Promise((res) => entry.file(res, () => res(null)));
        if (!f) return;
        if (/\.zip$/i.test(f.name)) { zips.push(f); return; }
        const rel = path ? path + "/" + f.name : f.name;
        Object.defineProperty(f, "webkitRelativePath", { value: rel, configurable: true });
        collected.push(f);
      } else if (entry.isDirectory) {
        const reader = entry.createReader();
        let batch;
        do {
          batch = await new Promise((res) => reader.readEntries(res, () => res([])));
          for (const en of batch) await walk(en, path ? path + "/" + en.name : en.name);
        } while (batch.length);
      }
    }

    for (const entry of entries) await walk(entry, "");
    for (const f of plain) {
      if (/\.zip$/i.test(f.name)) zips.push(f);
      else collected.push(f);
    }

    if (zips.length) {
      const expanded = [];
      for (const z of zips) {
        try {
          expanded.push(...(await expandZip(z)));
        } catch (err) {
          log("Ошибка: " + err.message, "err");
        }
      }
      if (expanded.length) {
        files = dedupe([...collected, ...expanded]);
        log("> Из zip извлечено файлов: " + expanded.length, "dim");
      } else {
        files = dedupe(collected);
      }
    } else {
      files = dedupe(collected);
    }
    if (!files.length) log("> Не удалось прочитать перетащенные элементы", "err");
    renderFileList();
  });

  els.zipInput.addEventListener("change", () => {
    const f = (els.zipInput.files || [])[0];
    els.zipInput.value = "";
    useZipFile(f);
  });

  els.folderInput.addEventListener("change", () => {
    files = Array.from(els.folderInput.files || []);
    renderFileList();
  });

  els.clear.addEventListener("click", () => {
    files = [];
    lastZip = null;
    resultZip = null;
    $("previews-grid").innerHTML = "";
    revokePreviewUrls();
    showPreviewsPanel(false);
    els.folderInput.value = "";
    els.zipInput.value = "";
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

  /* ---------------- per-file logging ---------------- */

  function skeletonNames() {
    const seen = new Set();
    const out = [];
    for (const f of files) {
      const name = (f.webkitRelativePath || f.name).split("/").pop();
      if (!SOURCE_RE.test(name)) continue;
      const base = name.replace(/\.(skel|json|txt|spine)$/i, "");
      if (!base || seen.has(base)) continue;
      seen.add(base);
      out.push(base);
    }
    return out;
  }

  /* ---------------- convert via broker -> Actions ---------------- */

  async function startConvertUrl(url) {
    const r = await fetch(BROKER + "/convert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
    return d.job;
  }

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

  async function waitStatus(job, t0, names) {
    const deadline = t0 + 12 * 60 * 1000;
    const list = Array.isArray(names) ? names : [];
    let lastLog = 0;
    const started = [];
    for (const n of list) {
      log("… конвертация " + n + "…", "dim");
      started.push(n);
    }
    while (Date.now() < deadline) {
      const r = await fetch(BROKER + "/status?job=" + encodeURIComponent(job), { method: "GET" });
      const d = await r.json().catch(() => ({}));
      if (d.ready) return;
      if (Date.now() - lastLog > 15000) {
        const sec = Math.round((Date.now() - t0) / 1000);
        log("… ждём GitHub Actions (" + sec + " c): выкачивание идёт в браузере, обычно 1–3 мин, максимум 9 мин…", "dim");
        lastLog = Date.now();
      }
      await sleep(3000);
    }
    throw new Error("Превышено время ожидания (12 минут)");
  }

  function logFromText(text, prefixRe, clsFn) {
    for (const line of text.split("\n")) {
      if (!line.trim()) continue;
      log(line, clsFn(line));
    }
  }

  async function fetchResult(job) {
    const r = await fetch(BROKER + "/download?job=" + encodeURIComponent(job), { method: "GET" });
    if (r.status === 202) throw new Error("результат ещё не опубликован — посмотрите историю");
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d.error || ("HTTP " + r.status));
    }
    return r.blob();
  }

  async function extractLog(name) {
    const z = await JSZip.loadAsync(lastZip);
    const f = z.file(name);
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

  els.histRefresh.addEventListener("click", () => {
    loadHistory();
    loadStats();
  });

  /* ---------------- summary ---------------- */

  async function logSummary() {
    try {
      const z = await JSZip.loadAsync(lastZip);
      let spine = 0, json = 0, skel = 0, img = 0, other = 0;
      const corruptFile = z.file("corrupt-list.txt");
      let corrupt = [];
      if (corruptFile) corrupt = (await corruptFile.async("string")).split("\n").filter(Boolean);
      z.forEach((path, file) => {
        if (file.dir || /\.tmp\.json$|\.v4\.json$|\.alt\.json$/.test(path)) return;
        if (path === "previews/index.json" || path === "previews/asset-manifest.json") return;
        if (/\.spine$/.test(path)) spine++;
        else if (/\.json$/.test(path)) json++;
        else if (/\.skel$/.test(path)) skel++;
        else if (/\.(png|jpg|jpeg|gif|webp|avif|bmp)$/i.test(path)) img++;
        else other++;
      });
      let line = "итого: .spine " + spine + " · json " + json + " · skel " + skel +
                 " · картинок " + img;
      if (corrupt.length) line += " · битых файлов " + corrupt.length + " (конвертация невозможна)";
      log(line, "info");
      for (const c of corrupt.slice(0, 5)) log("  битый: " + c, "err");
      if (corrupt.length > 5) log("  … и ещё " + (corrupt.length - 5) + " битых (см. corrupt-list.txt)", "err");
    } catch (e) {
      /* сводка не критична */
    }
  }

  /* ---------------- скриншоты компиляции ---------------- */

  const IMG_RE = /\.(png|jpe?g|webp|gif|avif|bmp|tiff?)$/i;
  const BUNDLE_MAX_BYTES = 256 * 1048576;
  const BUNDLE_MAX_FILES = 2000;
  let resultZip = null;
  let panelTimer = 0;
  const previewUrls = [];

  function dirOf(path) {
    const i = path.lastIndexOf("/");
    return i < 0 ? "" : path.slice(0, i);
  }

  function baseOf(path) {
    const i = path.lastIndexOf("/");
    return i < 0 ? path : path.slice(i + 1);
  }

  function stemOf(path) {
    const b = baseOf(path);
    const i = b.lastIndexOf(".");
    return i <= 0 ? b : b.slice(0, i);
  }

  function fmtSize(b) {
    if (!b && b !== 0) return "";
    if (b < 1024) return b + " Б";
    if (b < 1048576) return (b / 1024).toFixed(1) + " КБ";
    return (b / 1048576).toFixed(1) + " МБ";
  }

  function revokePreviewUrls() {
    while (previewUrls.length) {
      try { URL.revokeObjectURL(previewUrls.pop()); } catch (e) { /* ignore */ }
    }
  }

  function announce(text) {
    let live = $("live");
    if (!live) {
      live = document.createElement("div");
      live.id = "live";
      live.setAttribute("role", "status");
      live.setAttribute("aria-live", "polite");
      live.className = "sr-only";
      document.body.appendChild(live);
    }
    live.textContent = text;
  }

  function showPreviewsPanel(on) {
    const panel = $("previews-panel");
    const pick = document.querySelector("section.pick");
    if (panelTimer) { clearTimeout(panelTimer); panelTimer = 0; }
    if (on) {
      panel.classList.remove("hidden", "out");
      requestAnimationFrame(() => requestAnimationFrame(() => panel.classList.add("in")));
      if (pick) {
        pick.style.transition = "opacity .35s ease, transform .35s ease";
        pick.style.opacity = "0";
        pick.style.transform = "translateY(-8px)";
        panelTimer = setTimeout(() => pick.classList.add("hidden"), 360);
      }
      if (!panel.hasAttribute("tabindex")) panel.setAttribute("tabindex", "-1");
      panel.focus({ preventScroll: true });
    } else {
      panel.classList.remove("in");
      panel.classList.add("out");
      panelTimer = setTimeout(() => {
        panel.classList.add("hidden");
        panel.classList.remove("out");
        const dz = $("dropzone");
        if (dz) dz.focus({ preventScroll: true });
      }, 380);
      if (pick) {
        pick.classList.remove("hidden");
        requestAnimationFrame(() => {
          pick.style.opacity = "1";
          pick.style.transform = "translateY(0)";
        });
      }
    }
  }

  /* --- состав комплекта одного скелета --- */

  function buildIndexes() {
    const byPath = new Map();
    const byBase = new Map();
    Object.keys(resultZip.files).forEach((name) => {
      const f = resultZip.files[name];
      if (f.dir) return;
      byPath.set(name, f);
      const key = baseOf(name).toLowerCase();
      if (IMG_RE.test(key)) {
        if (!byBase.has(key)) byBase.set(key, []);
        byBase.get(key).push(name);
      }
    });
    return { byPath, byBase };
  }

  function atlasPagePaths(atlasPath, idx) {
    const out = [];
    let text = "";
    const f = idx.byPath.get(atlasPath);
    if (!f) return out;
    return f.async("string").then((t) => {
      text = t;
      const lines = text.split(/\r\n|\n|\r/).map((l) => l.trim()).filter(Boolean);
      for (let i = 0; i < lines.length; i++) {
        if (!IMG_RE.test(lines[i].toLowerCase())) continue;
        if (!(i + 1 < lines.length && lines[i + 1].indexOf("size:") === 0)) continue;
        const ref = lines[i];
        const dir = dirOf(atlasPath);
        const rel = dir ? dir + "/" + ref : ref;
        if (idx.byPath.has(rel)) { out.push(rel); continue; }
        const sib = dir ? dir + "/" + baseOf(ref) : baseOf(ref);
        if (idx.byPath.has(sib)) { out.push(sib); continue; }
        const hits = idx.byBase.get(baseOf(ref).toLowerCase()) || [];
        if (hits.length === 1) out.push(hits[0]);
      }
      return out;
    });
  }

  function collectBundle(item) {
    const idx = buildIndexes();
    const spine = idx.byPath.has(item.spine) ? item.spine : item.spine.replace(/\.json$/i, ".spine");
    if (!idx.byPath.has(spine)) return Promise.reject(new Error("в архиве нет " + baseOf(spine)));
    const dir = dirOf(spine);
    const stem = stemOf(spine);
    const set = new Set([spine]);
    [".json", ".skel"].forEach((e) => {
      const p = dir ? dir + "/" + stem + e : stem + e;
      if (idx.byPath.has(p)) set.add(p);
    });
    const atlases = Object.keys(idx.byPath).filter((n) => {
      if (dirOf(n) !== dir) return false;
      return n.toLowerCase().endsWith(".atlas") || n.toLowerCase().endsWith(".atlas.txt");
    });
    const preferred = atlases.filter((n) => stemOf(n) === stem);
    const use = preferred.length ? preferred : atlases;
    use.forEach((n) => set.add(n));
    return Promise.all(use.map((n) => atlasPagePaths(n, idx))).then((pages) => {
      pages.forEach((list) => list.forEach((p) => set.add(p)));
      if (!pages.some((l) => l.length)) {
        Object.keys(idx.byPath).forEach((n) => {
          if (dirOf(n) === dir && IMG_RE.test(n)) set.add(n);
        });
      }
      if (set.size > BUNDLE_MAX_FILES) {
        return Promise.reject(new Error("в комплекте " + set.size + " файлов — скачайте общий архив"));
      }
      let total = 0;
      set.forEach((n) => { total += (idx.byPath.get(n)._data && idx.byPath.get(n)._data.uncompressedSize) || 0; });
      if (total > BUNDLE_MAX_BYTES) {
        return Promise.reject(new Error("комплект " + fmtSize(total) + " — скачайте общий архив"));
      }
      const out = new JSZip();
      const jobs = [];
      set.forEach((n) => {
        jobs.push(idx.byPath.get(n).async("uint8array").then((buf) => {
          out.file(n, buf, { createFolders: false, binary: true });
        }));
      });
      return Promise.all(jobs).then(() => out.generateAsync({ type: "blob", compression: "DEFLATE" }));
    });
  }

  function saveBlob(blob, filename) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }

  function showFetchVerdict(text) {
    const box = $("fetch-verdict");
    if (!box) return;
    box.textContent = text;
    box.classList.remove("hidden");
  }

  async function renderPreviews(blob) {
    const grid = $("previews-grid");
    revokePreviewUrls();
    grid.innerHTML = "";
    resultZip = null;
    let z;
    try {
      z = await JSZip.loadAsync(blob);
    } catch (e) {
      return false;
    }
    const report = z.file("fetch-report.json");
    if (report) {
      try {
        const r = JSON.parse(await report.async("string"));
        if (r && r.ok === false) {
          showFetchVerdict(
            "Выкачивание не удалось: " + (r.verdict || "причина неизвестна") +
            (r.http_status ? " (HTTP " + r.http_status + ")" : "") +
            (r.antibot && r.antibot.length ? ". Антибот: " + r.antibot.slice(0, 3).join(", ") : "")
          );
        }
      } catch (e) { /* отчёт не читается — не критично */ }
    }
    const idxFile = z.file("previews/index.json");
    if (!idxFile) { showPreviewsPanel(false); return false; }
    let items = [];
    try {
      const raw = JSON.parse(await idxFile.async("string"));
      items = Array.isArray(raw.items) ? raw.items : [];
    } catch (e) {
      items = [];
    }
    items = items.filter((it) => it && typeof it.png === "string" && typeof it.spine === "string" && z.file(it.png));
    if (!items.length) { showPreviewsPanel(false); return false; }
    resultZip = z;

    const frag = document.createDocumentFragment();
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      const label = it.name || stemOf(it.spine);
      const card = document.createElement("div");
      card.className = "pv-card";
      card.setAttribute("role", "listitem");
      card.style.animationDelay = Math.min(i * 45, 600) + "ms";

      const shot = document.createElement("div");
      shot.className = "pv-shot";

      const tip = document.createElement("div");
      tip.className = "pv-tip";
      [label + ".spine", "путь: " + it.spine,
       "кадр: " + (it.kind === "atlas" ? "текстура атласа" : "рендер Spine"),
       "размер: " + fmtSize(it.bytes)].forEach((line, n) => {
        if (n) tip.appendChild(document.createElement("br"));
        tip.appendChild(document.createTextNode(line));
      });
      shot.appendChild(tip);

      const pngFile = z.file(it.png);
      if (pngFile) {
        pngFile.async("blob").then((b) => {
          const url = URL.createObjectURL(b);
          previewUrls.push(url);
          const img = document.createElement("img");
          img.alt = "Превью: " + label + ".spine";
          const drop = () => {
            const k = previewUrls.indexOf(url);
            if (k >= 0) previewUrls.splice(k, 1);
            URL.revokeObjectURL(url);
          };
          img.addEventListener("load", drop, { once: true });
          img.addEventListener("error", drop, { once: true });
          shot.appendChild(img);
        }).catch(() => { /* карточка останется без картинки */ });
      }

      const meta = document.createElement("div");
      meta.className = "pv-meta";
      const name = document.createElement("span");
      name.className = "pv-name";
      name.textContent = label + ".spine";
      name.title = it.spine;
      const kind = document.createElement("span");
      kind.className = "pv-kind" + (it.kind === "atlas" ? " atlas" : "");
      kind.textContent = it.kind === "atlas" ? "атлас" : "рендер";
      meta.appendChild(name);
      meta.appendChild(kind);

      const actions = document.createElement("div");
      actions.className = "pv-actions";

      const withImg = document.createElement("button");
      withImg.className = "btn ghost small";
      withImg.textContent = "Скачать с картинками";
      withImg.setAttribute("aria-label", "Скачать " + label + ".spine вместе с текстурами");
      withImg.addEventListener("click", async () => {
        withImg.setAttribute("aria-disabled", "true");
        withImg.setAttribute("aria-busy", "true");
        const old = withImg.textContent;
        withImg.textContent = "Собираем…";
        try {
          const out = await collectBundle(it);
          saveBlob(out, label + "-spine.zip");
          log("… превью: комплект " + label + " собран", "dim");
        } catch (e) {
          log("Превью: " + e.message, "err");
        } finally {
          withImg.removeAttribute("aria-disabled");
          withImg.removeAttribute("aria-busy");
          withImg.textContent = old;
        }
      });

      const full = document.createElement("button");
      full.className = "btn ghost small";
      full.textContent = "Только .spine";
      full.setAttribute("aria-label", "Скачать только " + label + ".spine");
      full.addEventListener("click", async () => {
        try {
          const src = resultZip && resultZip.file(it.spine.replace(/\.json$/i, ".spine"));
          if (!src) { log("Превью: .spine не найден в архиве", "err"); return; }
          const out = new JSZip();
          out.file(baseOf(it.spine).replace(/\.json$/i, ".spine"), await src.async("uint8array"));
          saveBlob(await out.generateAsync({ type: "blob" }), label + ".spine");
        } catch (e) {
          log("Превью: " + e.message, "err");
        }
      });

      actions.appendChild(withImg);
      actions.appendChild(full);
      card.appendChild(shot);
      card.appendChild(meta);
      card.appendChild(actions);
      frag.appendChild(card);
    }
    grid.appendChild(frag);

    showPreviewsPanel(true);
    announce("Показано скриншотов компиляции: " + items.length);
    return true;
  }

  $("previews-back").addEventListener("click", () => showPreviewsPanel(false));


  /* ---------------- start / download ---------------- */

  els.start.addEventListener("click", async () => {
    els.start.disabled = true;
    els.download.classList.add("hidden");
    lastZip = null;
    const url = (els.srcUrl && els.srcUrl.value || "").trim();
    resultZip = null;
    $("previews-grid").innerHTML = "";
    revokePreviewUrls();
    showPreviewsPanel(false);
    els.log.textContent = "";
    let job = null;
    const t0 = Date.now();
    try {
      if (url) {
        setStatus("скачиваем ассеты…", "run");
        log("> Ссылка: " + url, "dim");
        log("> Загружаю манифест игры и тяну скелеты, атласы и текстуры…", "dim");
        job = await startConvertUrl(url);
        log("> Задача: " + job + " (режим: ссылка)", "dim");
        setStatus("скачиваем и конвертируем…", "run");
      } else {
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
        const names = skeletonNames();
        log("> Отправляем " + files.length + " файлов, задач: " + names.length + "…", "dim");
        job = await startConvert(blob);
        log("> Задача: " + job, "dim");
        setStatus("конвертация…", "run");
      }
      const taskNames = url ? [] : skeletonNames();
      if (url) log("… идёт выкачивание и конвертация, следим за логом задачи…", "dim");
      await waitStatus(job, t0, taskNames);
      log("> Результат готов, скачиваем…", "dim");
      lastZip = await fetchResult(job);
      if (url) log("… ассеты игры скачаны и сконвертированы", "ok");

      const unpackText = await extractLog("unpack-log.txt");
      if (unpackText) {
        const m = /распаковано (\d+) картинок/.exec(unpackText);
        if (m) log("… распаковано картинок: " + m[1] + "…", "dim");
      }
      const convertText = await extractLog("convert-log.txt");
      if (convertText) {
        logFromText(convertText, /^OK|^KEEP|^FAIL|^done|^repair/, (line) =>
          /^FAIL/.test(line) ? "err"
            : /лечение/.test(line) ? "ok"
            : /^OK/.test(line) ? "ok" : /^done|^repair/.test(line) ? "info" : "dim");
      }
      const compileText = await extractLog("compile-log.txt");
      if (compileText) {
        for (const line of compileText.split("\n")) {
          if (!line.trim() || /^\s{2}/.test(line)) continue;
          if (/^compile-block: ✓/.test(line)) log("… .spine готов: " + (line.split("→")[1] || "").trim().split(" ")[0] + "…", "ok");
          else if (/^compile-block: FAIL/.test(line)) log(line, "err");
          else if (/^compile-block: итого|^compile-block: скелетов|^compile-block: normalize/.test(line)) log(line, "info");
        }
      }
      await logSummary();
      setStatus("готово", "ok");
      els.download.classList.remove("hidden");
      let hasPreviews = false;
      try {
        hasPreviews = await renderPreviews(lastZip);
      } catch (e) {
        log("Превью: " + e.message, "err");
      }
      if (hasPreviews) log("… скриншотов компиляции: " + $("previews-grid").children.length, "dim");
      loadHistory();
      loadStats();
    } catch (e) {
      log("Ошибка: " + e.message, "err");
      setStatus("ошибка", "err");
    } finally {
      els.start.disabled = false;
    }
  });

  syncStart();

  if (els.urlClear) {
    els.urlClear.addEventListener("click", () => {
      if (els.srcUrl) {
        els.srcUrl.value = "";
        els.srcUrl.focus();
        syncStart();
      }
    });
  }
  if (els.srcUrl) {
    els.srcUrl.addEventListener("input", syncStart);
    els.srcUrl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && els.srcUrl.value.trim() && !els.start.disabled) {
        e.preventDefault();
        els.start.click();
      }
    });
  }

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
  registerVisit().then(loadStats);
})();
