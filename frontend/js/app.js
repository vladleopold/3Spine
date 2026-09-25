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
    const supported = files.filter((f) => INPUT_RE.test(f.name));
    els.fileSummary.classList.toggle("hidden", files.length === 0);
    els.fileCount.textContent = files.length + " (источников: " + skels.length + ")";
    els.start.disabled = supported.length === 0;
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
    const list = names || [];
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
        log("… ждём GitHub Actions (" + sec + " c), в очереди: " + started.length + " шт.…", "dim");
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

  const IMG_RE = /\.(png|jpe?g|webp|gif|avif)$/i;
  let resultZip = null;

  function dirOf(path) {
    const i = path.lastIndexOf("/");
    return i < 0 ? "" : path.slice(0, i);
  }

  function fmtSize(b) {
    if (!b && b !== 0) return "";
    if (b < 1024) return b + " Б";
    if (b < 1048576) return (b / 1024).toFixed(1) + " КБ";
    return (b / 1048576).toFixed(1) + " МБ";
  }

  function showPreviewsPanel(on) {
    const panel = $("previews-panel");
    const pick = document.querySelector("section.pick");
    if (on) {
      panel.classList.remove("hidden");
      requestAnimationFrame(() => panel.classList.add("in"));
      if (pick) {
        pick.style.transition = "opacity .35s ease, transform .35s ease";
        pick.style.opacity = "0";
        pick.style.transform = "translateY(-8px)";
        setTimeout(() => pick.classList.add("hidden"), 360);
      }
    } else {
      panel.classList.remove("in");
      panel.classList.add("out");
      setTimeout(() => {
        panel.classList.add("hidden");
        panel.classList.remove("out");
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

  function buildPreviewBundle(item) {
    const z = resultZip;
    const base = dirOf(item.spine);
    const stem = item.spine.split("/").pop().replace(/\.spine$/i, "");
    const out = new JSZip();
    let total = 0;
    Object.keys(z.files).forEach((name) => {
      const f = z.files[name];
      if (f.dir) return;
      const inBase = dirOf(name) === base;
      const inImages = base && name.indexOf(base + "/images/") === 0;
      const isSpine = name === item.spine;
      const isMeta = inBase && new RegExp("^" + stem.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\.(json|atlas|atlas\\.txt)$", "i").test(name.split("/").pop());
      if (!isSpine && !isMeta && !inImages && !(inBase && IMG_RE.test(name))) return;
      out.file(name, f, { binary: true });
      total += (f._data && f._data.uncompressedSize) || 0;
    });
    if (total > 600 * 1048576) {
      throw new Error("набор больше 600 МБ — скачайте общий архив");
    }
    return out.generateAsync({ type: "blob", compression: "DEFLATE" });
  }

  async function renderPreviews(blob) {
    const grid = $("previews-grid");
    grid.innerHTML = "";
    let z;
    try {
      resultZip = await JSZip.loadAsync(blob);
    } catch (e) {
      return false;
    }
    const idxFile = z.file("previews/index.json");
    if (!idxFile) {
      showPreviewsPanel(false);
      return false;
    }
    let items = [];
    try {
      items = (JSON.parse(await idxFile.async("string")).items || []);
    } catch (e) {
      items = [];
    }
    if (!items.length) {
      showPreviewsPanel(false);
      return false;
    }

    const frag = document.createDocumentFragment();
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      const card = document.createElement("div");
      card.className = "pv-card";
      card.style.animationDelay = Math.min(i * 45, 600) + "ms";

      const shot = document.createElement("div");
      shot.className = "pv-shot";
      const tip = document.createElement("div");
      tip.className = "pv-tip";
      tip.innerHTML = "<b>" + it.name + ".spine</b><br>путь: " + it.spine +
        "<br>кадр: " + (it.kind === "atlas" ? "текстура атласа" : "рендер Spine") +
        "<br>размер: " + fmtSize(it.bytes);
      shot.appendChild(tip);

      const png = z.file(it.png);
      if (png) {
        const url = URL.createObjectURL(await png.async("blob"));
        const img = document.createElement("img");
        img.src = url;
        img.alt = it.name;
        img.addEventListener("load", () => URL.revokeObjectURL(url), { once: true });
        shot.appendChild(img);
      }

      const meta = document.createElement("div");
      meta.className = "pv-meta";
      const name = document.createElement("span");
      name.className = "pv-name";
      name.textContent = it.name + ".spine";
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
      withImg.addEventListener("click", async () => {
        withImg.disabled = true;
        const old = withImg.textContent;
        withImg.textContent = "Собираем…";
        try {
          const out = await buildPreviewBundle(it);
          const a = document.createElement("a");
          a.href = URL.createObjectURL(out);
          a.download = it.name + "-spine.zip";
          document.body.appendChild(a);
          a.click();
          setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
        } catch (e) {
          log("Превью: " + e.message, "err");
        } finally {
          withImg.disabled = false;
          withImg.textContent = old;
        }
      });
      const full = document.createElement("button");
      full.className = "btn ghost small";
      full.textContent = ".spine";
      full.title = "Скачать только .spine";
      full.addEventListener("click", async () => {
        try {
          const src = resultZip.file(it.spine);
          if (!src) return;
          const out = new JSZip();
          out.file(it.spine.split("/").pop(), await src.async("blob"));
          const blobOut = await out.generateAsync({ type: "blob" });
          const a = document.createElement("a");
          a.href = URL.createObjectURL(blobOut);
          a.download = it.name + ".spine";
          document.body.appendChild(a);
          a.click();
          setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
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
    return true;
  }

  $("previews-back").addEventListener("click", () => showPreviewsPanel(false));

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
      const names = skeletonNames();
      log("> Отправляем " + files.length + " файлов, задач: " + names.length + "…", "dim");
      const job = await startConvert(blob);
      log("> Задача: " + job, "dim");
      setStatus("конвертация…", "run");
      await waitStatus(job, t0, names);
      log("> Результат готов, скачиваем…", "dim");
      lastZip = await fetchResult(job);

      const unpackText = await extractLog("unpack-log.txt");
      if (unpackText) {
        const m = /распаковано (\d+) картинок/.exec(unpackText);
        if (m) log("… распаковано картинок: " + m[1] + "…", "dim");
      }
      const convertText = await extractLog("convert-log.txt");
      if (convertText) {
        logFromText(convertText, /^OK|^KEEP|^FAIL|^done/, (line) =>
          /^FAIL/.test(line) ? "err" : /^OK/.test(line) ? "ok" : /^done/.test(line) ? "info" : "dim");
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
      const hasPreviews = await renderPreviews(lastZip);
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
