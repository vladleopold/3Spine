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

  /* ---------------- панель текущего прохода ---------------- */

  const run = { job: "", t0: 0, timer: 0, step: "", logTimerLine: null, finalReady: false };

  function setRunStep(text) {
    run.step = text || "";
    const el = $("run-step");
    if (el) el.textContent = run.step;
  }

  function fmtElapsed(ms) {
    const s = Math.max(0, Math.round(ms / 1000));
    return s < 60 ? s + " c" : Math.floor(s / 60) + " мин " + (s % 60) + " c";
  }

  function startRunTicker() {
    stopRunTicker();
    if (!run.t0) run.t0 = Date.now();
    run.timer = setInterval(() => {
      const txt = fmtElapsed(Date.now() - run.t0);
      const el = $("run-elapsed");
      if (el) el.textContent = txt;
      const m = document.querySelector("#history-list .history-item .h-meta");
      if (m && run.job) m.textContent = "сейчас идёт · прошло " + txt;
      if (run.logTimerLine && els.log.contains(run.logTimerLine)) {
        run.logTimerLine.textContent = "… ждём GitHub Actions · " + txt;
      }
    }, 500);
  }

  function stopRunTicker() {
    if (run.timer) { clearInterval(run.timer); run.timer = 0; }
  }

  // Панель прохода занимает место панели загрузки: drag-and-drop исчезает.
  function showRunPanel(on, opts) {
    const panel = $("run-panel");
    const pick = document.querySelector("section.pick");
    document.body.classList.toggle("running", !!on);
    if (on) {
      const o = opts || {};
      run.job = o.job || run.job || "";
      const j = $("run-job");
      if (j) j.textContent = run.job;
      const t = $("run-title");
      if (t) t.textContent = o.title || "Проход запущен";
      if (o.step) setRunStep(o.step);
      panel.classList.remove("hidden");
      if (pick) {
        pick.style.transition = "opacity .3s ease, transform .3s ease";
        pick.style.opacity = "0";
        pick.style.transform = "translateY(-8px)";
        setTimeout(() => pick.classList.add("hidden"), 320);
      }
      startRunTicker();
    } else {
      panel.classList.add("hidden");
      if (pick) {
        pick.classList.remove("hidden");
        requestAnimationFrame(() => {
          pick.style.opacity = "1";
          pick.style.transform = "translateY(0)";
        });
      }
      stopRunTicker();
    }
  }

  function markRunDone() {
    document.body.classList.remove("running");
    stopRunTicker();
  }

  /* ---------------- UI helpers ---------------- */

  function log(text, cls) {
    const span = document.createElement("span");
    if (cls) span.className = cls;
    span.textContent = text;
    els.log.appendChild(span);
    els.log.append("\n");
    els.log.scrollTop = els.log.scrollHeight;
    return span;
  }

  function startLogTimer() {
    if (run.logTimerLine && els.log.contains(run.logTimerLine)) return;
    run.logTimerLine = log("… ждём GitHub Actions · " + fmtElapsed(Date.now() - run.t0), "dim");
  }

  function finishLogTimer(text, cls) {
    if (run.logTimerLine && els.log.contains(run.logTimerLine)) {
      run.logTimerLine.textContent = text;
      run.logTimerLine.className = cls || "";
    }
    run.logTimerLine = null;
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

  // Карточки показываем сразу после обнаружения Spine-пар, не дожидаясь
  // компиляции: CI кладёт сырой архив в history/<job>-raw.zip, как только
  // экстрактор закончил, а сайт открывает архив по имени. Потом, когда
  // соберётся .spine, карточки перезальются уже готовыми.
  async function showRawCards(job, t0) {
    const deadline = t0 + 18 * 60 * 1000;
    while (Date.now() < deadline && run.job === job && !run.finalReady) {
      await sleep(4000);
      let r;
      try {
        r = await fetch(BROKER + "/download?archive=" + encodeURIComponent(job + "-raw"), {
          method: "GET", cache: "no-store",
        });
      } catch (e) { continue; }
      if (!r.ok) continue;
      const blob = await r.blob().catch(() => null);
      if (!blob || blob.size < 2048) continue;
      if (run.finalReady || run.job !== job) return false;
      try {
        if (!(await renderPreviews(blob))) continue;
        const n = $("previews-grid").children.length;
        log("… Spine-пар найдено: " + n + " анимаций. Показываю их до компиляции…", "ok");
        setRunStep("Найдено анимаций: " + n + ". Компиляция .spine ещё идёт — карточки обновятся.");
        showPreviewsPanel(true);
      } catch (e) {
        log("Сырой архив открылся не полностью (" + e.message + "), ждём компиляцию…", "dim");
      }
      return true;
    }
    return false;
  }

  async function waitStatus(job, t0, names) {
    const deadline = t0 + 20 * 60 * 1000;
    const list = Array.isArray(names) ? names : [];
    const started = [];
    for (const n of list) {
      log("… конвертация " + n + "…", "dim");
      started.push(n);
    }
    startLogTimer();
    let failures = 0;
    while (Date.now() < deadline) {
      try {
        const ctl = new AbortController();
        const timeout = setTimeout(() => ctl.abort(), 12000);
        const r = await fetch(
          BROKER + "/status?job=" + encodeURIComponent(job) + "&_=" + Date.now(),
          { method: "GET", cache: "no-store", signal: ctl.signal }
        ).finally(() => clearTimeout(timeout));
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error("HTTP " + r.status);
        failures = 0;
        if (d.ready) {
          finishLogTimer("… GitHub Actions завершён · " + fmtElapsed(Date.now() - t0), "ok");
          return;
        }
      } catch (_) {
        // Сеть, GitHub API и Worker иногда кратко отвечают 5xx/timeout.
        // Не обрываем проход: следующая проверка продолжится автоматически.
        failures++;
        if (failures >= 3) setRunStep("Связь временно прервалась — продолжаю проверять результат автоматически…");
      }
      await sleep(3000);
    }
    finishLogTimer("… GitHub Actions: превышено время ожидания", "err");
    throw new Error("Превышено время ожидания (20 минут)");
  }

  function logFromText(text, prefixRe, clsFn) {
    for (const line of text.split("\n")) {
      if (!line.trim()) continue;
      log(line, clsFn(line));
    }
  }

  async function fetchResult(job) {
    let lastError = null;
    for (let attempt = 0; attempt < 8; attempt++) {
      try {
        const r = await fetch(
          BROKER + "/download?job=" + encodeURIComponent(job) + "&_=" + Date.now(),
          { method: "GET", cache: "no-store" }
        );
        if (r.status === 202) throw new Error("результат ещё публикуется");
        if (!r.ok) {
          const d = await r.json().catch(() => ({}));
          throw new Error(d.error || ("HTTP " + r.status));
        }
        return r.blob();
      } catch (e) {
        lastError = e;
        if (attempt < 7) await sleep(2500);
      }
    }
    throw lastError || new Error("результат не удалось скачать");
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

  let activeJob = "";

  function setHistoryBusy(row, on) {
    if (!row) return;
    row.classList.toggle("h-item-loading", !!on);
    if (on) row.setAttribute("aria-busy", "true");
    else row.removeAttribute("aria-busy");
    let spin = row.querySelector(".h-spinner");
    if (on && !spin) {
      spin = document.createElement("span");
      spin.className = "h-spinner";
      spin.setAttribute("aria-hidden", "true");
      row.appendChild(spin);
    } else if (!on && spin) {
      spin.remove();
    }
  }

  // Открыть сохранённый проход: тянем его архив и показываем анимации.
  async function openJobArchive(job, row) {
    if (!job) return;
    activeJob = job;
    setHistoryBusy(row, true);
    setStatus("открываю " + job + "…", "run");
    try {
      const r = await fetch(
        BROKER + "/download?archive=" + encodeURIComponent(job) + "&_=" + Date.now(),
        { method: "GET", cache: "no-store" }
      );
      if (!r.ok) throw new Error("HTTP " + r.status);
      const blob = await r.blob();
      lastZip = blob;
      els.download.href = BROKER + "/download?archive=" + encodeURIComponent(job);
      els.download.classList.remove("hidden");
      const okPv = await renderPreviews(blob);
      if (!okPv) {
        showFetchVerdict("В этом проходе нет превью анимаций — скачайте архив целиком.");
        showPreviewsPanel(false);
      }
      setStatus("готово", "ok");
      log("> Открыт проход " + job + (okPv ? ": анимаций " + $("previews-grid").children.length : ""), "ok");
    } catch (e) {
      log("Не удалось открыть проход " + job + ": " + e.message, "err");
      setStatus("ошибка", "err");
    } finally {
      setHistoryBusy(row, false);
    }
    loadHistory();
  }

  function renderRunningItem() {
    if (!run.job) return null;
    const row = document.createElement("div");
    row.className = "history-item h-item-clickable h-item-active";
    const job = document.createElement("div");
    job.className = "h-job";
    job.textContent = run.job;
    job.title = "Текущий проход — клик покажет лог";
    const tag = document.createElement("span");
    tag.className = "h-tag in-progress";
    tag.textContent = "in_progress";
    const meta = document.createElement("div");
    meta.className = "h-meta";
    meta.textContent = "сейчас идёт · прошло " + fmtElapsed(Date.now() - run.t0);
    row.appendChild(job);
    row.appendChild(tag);
    row.appendChild(meta);
    row.addEventListener("click", () => {
      showPreviewsPanel(false);
      showRunPanel(true, { job: run.job, title: "Проход выполняется", step: run.step || "Идёт сборка в GitHub Actions…" });
      els.log.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    return row;
  }

  function renderHistory(items) {
    els.histList.innerHTML = "";
    const running = renderRunningItem();
    if (running) els.histList.appendChild(running);
    if (!items.length && !running) {
      const d = document.createElement("div");
      d.className = "history-empty";
      d.textContent = "история пока пуста — архивы появятся после первой конвертации";
      els.histList.appendChild(d);
      return;
    }
    for (const it of items) {
      if (it.job === run.job) continue;
      const row = document.createElement("div");
      row.className = "history-item h-item-clickable" + (it.job === activeJob ? " h-item-active" : "");
      row.setAttribute("role", "button");
      row.setAttribute("tabindex", "0");

      const job = document.createElement("div");
      job.className = "h-job";
      job.textContent = it.title || it.label || it.name || it.job;
      job.title = (it.title ? it.job + " · " : "") +
        (it.file || it.job) + " — клик покажет анимации этого прохода";

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
      a.textContent = "Весь пакет";
      a.setAttribute("download", "");
      a.addEventListener("click", (ev) => {
        ev.stopPropagation();
        setHistoryBusy(row, true);
        a.classList.add("h-download-busy");
        a.setAttribute("aria-busy", "true");
        const old = a.textContent;
        a.textContent = "Скачиваю…";
        setTimeout(() => {
          if (!row.isConnected) return;
          setHistoryBusy(row, false);
          a.classList.remove("h-download-busy");
          a.removeAttribute("aria-busy");
          a.textContent = old;
        }, 3500);
      });
      row.appendChild(a);

      row.addEventListener("click", (ev) => {
        if (ev.target === a || a.contains(ev.target)) return;   // это ссылка на весь архив
        openJobArchive(it.job, row);
      });
      row.addEventListener("keydown", (ev) => {
        if (ev.key !== "Enter" && ev.key !== " ") return;
        ev.preventDefault();
        openJobArchive(it.job, row);
      });
      els.histList.appendChild(row);
    }
  }

  async function loadHistory() {
    try {
      const r = await fetch(BROKER + "/history?_=" + Date.now(), { method: "GET", cache: "no-store" });
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

  els.histRefresh.addEventListener("click", async () => {
    const old = els.histRefresh.textContent;
    els.histRefresh.disabled = true;
    els.histRefresh.classList.add("btn-loading");
    els.histRefresh.setAttribute("aria-busy", "true");
    els.histRefresh.textContent = "Обновляю…";
    await Promise.allSettled([loadHistory(), loadStats()]);
    els.histRefresh.disabled = false;
    els.histRefresh.classList.remove("btn-loading");
    els.histRefresh.removeAttribute("aria-busy");
    els.histRefresh.textContent = old;
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

  // Остановленные кадры скелетов рисуем ПАРАЛЛЕЛЬНО: карточек бывает 60+, и по
  // одной они проявлялись бы десятки секунд. Ассеты загружаются только после
  // попадания задачи в свободный слот: раньше collectSpineAssets запускался
  // сразу для ВСЕХ карточек и мог заморозить вкладку на больших архивах.
  const STILL_CONCURRENCY = 4;
  const STILL_TIMEOUT = 25000;
  let stillBatch = null;
  function setNote(info, text) {
    if (!info) return;
    let line = info.querySelector(".pv-note");
    if (text) {
      if (!line) {
        line = document.createElement("div");
        line.className = "pv-note";
        info.appendChild(line);
      }
      line.textContent = text;
    } else if (line) {
      line.remove();
    }
  }
  function withTimeout(promise, ms) {
    let timer = 0;
    return Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("таймаут превью")), ms);
      }),
    ]).finally(() => clearTimeout(timer));
  }

  function newStillBatch() {
    if (stillBatch) {
      stillBatch.cancelled = true;
      stillBatch.queue.length = 0;
    }
    stillBatch = { queue: [], busy: 0, cancelled: false };
    return stillBatch;
  }

  function pumpStills(batch) {
    if (!batch || batch.cancelled) return;
    while (batch.busy < STILL_CONCURRENCY && batch.queue.length) {
      const job = batch.queue.shift();
      batch.busy++;
      withTimeout(
        Promise.resolve()
          .then(() => job.loadAssets())
          .then((assets) => {
            if (batch.cancelled || !job.host.isConnected) return false;
            return window.SpineCardPlayer.still(job.host, Promise.resolve(assets));
          }),
        STILL_TIMEOUT
      )
        .then((ok) => {
          if (!batch.cancelled && ok === false) setNote(job.info, "не удалось показать кадр");
        })
        .catch((e) => {
          if (!batch.cancelled) setNote(job.info, "кадр: " + ((e && e.message) || e));
        })
        .finally(() => {
          job.card.classList.remove("pv-loading");
          job.card.removeAttribute("aria-busy");
          batch.busy--;
          pumpStills(batch);
        });
    }
  }

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

  function showPreviewsPanel(on, keepPick) {
    const panel = $("previews-panel");
    const pick = keepPick ? null : document.querySelector("section.pick");
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
    const sizeByPath = new Map();
    Object.keys(resultZip.files).forEach((name) => {
      const f = resultZip.files[name];
      if (f.dir) return;
      byPath.set(name, f);
      sizeByPath.set(name, (f._data && f._data.uncompressedSize) || 0);
      const key = baseOf(name).toLowerCase();
      if (IMG_RE.test(key)) {
        if (!byBase.has(key)) byBase.set(key, []);
        byBase.get(key).push(name);
      }
    });
    return { byPath, byBase, sizeByPath };
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

  function bundleProjectRoots(item, idx) {
    const label = (item.name || stemOf(item.spine || "")).toLowerCase();
    const roots = [];
    const add = (root) => {
      if (root && root !== "previews" && roots.indexOf(root) < 0) roots.push(root);
    };
    const addFrom = (path) => {
      const parts = path.split("/");
      if ((parts[0] === "spine" || parts[0] === "spine_projects") && parts.length > 2) {
        add(parts.slice(0, 2).join("/"));
      } else {
        add(dirOf(path));
      }
    };

    if (item.spine && idx.byPath.has(item.spine)) addFrom(item.spine);
    Object.keys(idx.byPath).forEach((name) => {
      if (stemOf(name).toLowerCase() !== label) return;
      if (!/\.(spine|skel|json|atlas|atlas\.txt)$/i.test(name)) return;
      addFrom(name);
    });
    ["spine/" + (item.name || ""), "spine_projects/" + (item.name || "")]
      .forEach((root) => {
        if (root !== "spine/" && root !== "spine_projects/" &&
            Array.from(idx.byPath.keys()).some((name) => name.indexOf(root + "/") === 0)) add(root);
      });
    return roots;
  }

  function collectBundle(item) {
    const idx = buildIndexes();
    const label = item.name || stemOf(item.spine || "animation");
    const labelLower = label.toLowerCase();
    const roots = bundleProjectRoots(item, idx);
    const underRoot = (name, root) => name.indexOf(root + "/") === 0;
    const inProject = (name) => roots.some((root) => underRoot(name, root));
    const set = new Set();

    // Берём настоящий проект, а не только маленькую копию из previews/.
    Object.keys(idx.byPath).forEach((name) => {
      if (!inProject(name) && name !== item.spine) return;
      if (/\.(spine|skel|json)$/i.test(name) && stemOf(name).toLowerCase() === labelLower) set.add(name);
    });
    if (item.spine && idx.byPath.has(item.spine)) set.add(item.spine);

    const atlases = Object.keys(idx.byPath).filter((name) =>
      inProject(name) && (/\.atlas$/i.test(name) || /\.atlas\.txt$/i.test(name))
    );
    const preferred = atlases.filter((name) => stemOf(name).toLowerCase() === labelLower);
    const use = preferred.length ? preferred : atlases;
    use.forEach((n) => set.add(n));
    return Promise.all(use.map((n) => atlasPagePaths(n, idx))).then((pages) => {
      pages.forEach((list) => list.forEach((p) => set.add(p)));

      // В ZIP из истории PNG могут лежать в нескольких вложенных папках:
      // images/<атлас>/, images/<проект>/, textures/ и рядом с атласом.
      // Для выбранного проекта включаем все изображения из его корня рекурсивно,
      // а не только изображения в одном ожидаемом пути.
      Object.keys(idx.byPath).forEach((name) => {
        if (!IMG_RE.test(name)) return;
        if (inProject(name)) set.add(name);
      });
      if (item.png && idx.byPath.has(item.png) && IMG_RE.test(item.png)) set.add(item.png);

      const pictures = Array.from(set).filter((name) => IMG_RE.test(name));
      if (!pictures.length) return Promise.reject(new Error("в комплекте не найдены картинки"));
      if (!Array.from(set).some((name) => /\.(spine|skel|json)$/i.test(name))) {
        return Promise.reject(new Error("в комплекте не найден скелет"));
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
      return Promise.all(jobs).then(() => out.generateAsync({ type: "blob", compression: "DEFLATE" }))
        .then((blob) => ({
          blob: blob,
          files: set.size,
          pictures: pictures.length,
          png: pictures.filter((name) => /\.png$/i.test(name)).length,
        }));
    });
  }

  async function collectReadyBundle(item) {
    const ready = item.zip && resultZip ? resultZip.file(item.zip) : null;
    if (!ready) throw new Error("готового комплекта нет");
    const bytes = await ready.async("uint8array");
    const nested = await JSZip.loadAsync(bytes);
    const names = Object.keys(nested.files).filter((name) => !nested.files[name].dir);
    const pictures = names.filter((name) => IMG_RE.test(name));
    if (!pictures.length) throw new Error("готовый комплект тоже не содержит картинок");
    return {
      blob: new Blob([bytes], { type: "application/zip" }),
      files: names.length,
      pictures: pictures.length,
      png: pictures.filter((name) => /\.png$/i.test(name)).length,
    };
  }

  // Ассеты для нативного проигрывания: skeleton JSON, его атлас и первая страница.
  // Пути ищем по имени карточки, потому что it.spine у Playson указывает на
  // превью, а сам скелет лежит в spine/<имя>/<имя>.json.
  async function collectSpineAssets(item) {
    const idx = buildIndexes();
    const label = item.name || stemOf(item.spine);
    // web-JSON — та же анимация, выгруженная редактором 4.3: её играет рантайм 4.3.
    // Атлас и страницы берём у исходного скелета, web-JSON лежит в отдельной папке.
    let skel = item.web && idx.byPath.has(item.web) ? item.web : null;
    const dirs = [dirOf(item.spine), "spine/" + label, ""].filter(
      (d, i, a) => a.indexOf(d) === i
    );
    if (!skel) for (const d of dirs) {
      const p = (d ? d + "/" : "") + label + ".json";
      if (idx.byPath.has(p)) { skel = p; break; }
    }
    if (!skel) {
      skel = Object.keys(idx.byPath).find(
        (n) => /\.json$/i.test(n) && stemOf(n) === label && !/index|manifest|report/i.test(n)
      );
    }
    if (!skel) throw new Error("в архиве нет " + label + ".json");
    const srcDir = dirs.find((d) => idx.byPath.has((d ? d + "/" : "") + label + ".json")) || dirOf(item.spine);
    const dir = srcDir || "";
    // Атлас не обязательно лежит рядом с выбранным JSON: у Playson скелет
    // дублируется в previews/, а атлас остаётся в spine/<имя>/. Поэтому ищем
    // по имени скелета сначала в родных папках, потом где угодно в архиве.
    const atlases = Object.keys(idx.byPath).filter((n) => /\.(atlas|atlas\.txt)$/i.test(n));
    const inDir = (d) => (d ? d + "/" : "") + label + ".atlas";
    const cands = [inDir(srcDir), inDir("spine/" + label), inDir(dir)];
    atlases.forEach((n) => {
      if (stemOf(n) === label) cands.push(n);
    });
    atlases.forEach((n) => {
      if (dirOf(n).split("/").pop() === label) cands.push(n);
    });
    atlases.forEach((n) => {
      if (dirOf(n) === srcDir) cands.push(n);
    });
    const atlas = cands.find((n) => idx.byPath.has(n));
    if (!atlas) throw new Error("в архиве нет атласа");
    const pages = await atlasPagePaths(atlas, idx);
    if (!pages.length) throw new Error("в архиве нет листов атласа");
    // У PragmaticPlay атлас часто многостраничный (wran_sansational — 17 листов),
    // поэтому лимит выше привычных 8. Мягкий потолок по весу отсекает совсем
    // безнадёжные случаи, чтобы не уронить вкладку.
    const MAX_PAGES = 32;
    if (pages.length > MAX_PAGES) throw new Error("в атласе слишком много листов (" + pages.length + ")");
    const totalBytes = pages.reduce((s, n) => s + (idx.sizeByPath.get(n) || 0), 0);
    if (totalBytes > 64 * 1024 * 1024) {
      throw new Error("страницы атласа слишком тяжёлые (" + Math.round(totalBytes / 1048576) + " МБ)");
    }
    const [json, atlasText, pageBlobs] = await Promise.all([
      idx.byPath.get(skel).async("string"),
      idx.byPath.get(atlas).async("string"),
      Promise.all(pages.map((n) => idx.byPath.get(n).async("blob"))),
    ]);
    return { json, atlas: atlasText, pages: pageBlobs, version: window.SpineCardPlayer ? window.SpineCardPlayer.version(json) : "3.8" };
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
    const batch = newStillBatch();
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
      card.className = "pv-card pv-loading";
      card.setAttribute("role", "listitem");
      card.setAttribute("aria-busy", "true");
      card.style.animationDelay = Math.min(i * 45, 600) + "ms";

      const shot = document.createElement("div");
      shot.className = "pv-shot";

      // Наведение курсора на карточку проигрывает анимацию нативно.
      if (window.SpineCardPlayer) {
        let filled = false;
        const fillAnims = (names) => {
          if (filled || !names || !names.length) return;
          filled = true;
          anim.querySelectorAll("option").forEach((o) => o.remove());
          names.forEach((n, k) => {
            const o = document.createElement("option");
            o.value = String(k);
            o.textContent = n;
            anim.appendChild(o);
          });
          anim.disabled = false;
          anim.value = String(window.SpineCardPlayer.current());
        };
        card.addEventListener("mouseenter", () => {
          window.SpineCardPlayer.play(shot, collectSpineAssets(it), fillAnims);
        });
        card.addEventListener("mouseleave", () => {
          window.SpineCardPlayer.stop();
          // если анимацию играть не вышло (пустая заглушка или не хватает
          // картинок в атласе) — подписываем это внизу карточки
          const note = window.SpineCardPlayer.note();
          let line = info.querySelector(".pv-note");
          if (note) {
            if (!line) {
              line = document.createElement("div");
              line.className = "pv-note";
              info.appendChild(line);
            }
            line.textContent = note;
          } else if (line) {
            line.remove();
          }
        });
      }

      // бывшие строки из тултипа — теперь мелким текстом внизу карточки
      const info = document.createElement("div");
      info.className = "pv-info";
      [it.spine,
       (it.kind === "atlas" ? "кадр: текстура атласа" : "кадр: рендер Spine") +
         " · " + fmtSize(it.bytes)].forEach((line) => {
        const d = document.createElement("div");
        d.textContent = line;
        info.appendChild(d);
      });

      // Скриншотов нет: в карточке сразу остановленный кадр скелета, а под
      // курсором он оживает. Все карточки стартуют параллельно (см. pumpStills).
      if (window.SpineCardPlayer && window.SpineCardPlayer.still) {
        batch.queue.push({
          host: shot,
          info: info,
          card: card,
          loadAssets: () => collectSpineAssets(it),
        });
      } else {
        card.classList.remove("pv-loading");
        card.removeAttribute("aria-busy");
      }

      const anim = document.createElement("select");
      anim.className = "pv-anim";
      anim.setAttribute("aria-label", "Анимация " + label);
      anim.disabled = true;
      const ph = document.createElement("option");
      ph.textContent = "наведи на карточку";
      ph.value = "";
      anim.appendChild(ph);
      anim.addEventListener("change", () => {
        if (anim.value !== "" && window.SpineCardPlayer) window.SpineCardPlayer.select(anim.value);
      });
      anim.addEventListener("click", (e) => e.stopPropagation());
      info.appendChild(anim);

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
          let out;
          try {
            out = await collectBundle(it);
          } catch (primaryError) {
            // Старые провайдеры иногда хранят комплект только во вложенном
            // pairs/*.zip. Он остаётся запасным путём, но скачивается лишь
            // после проверки, что внутри действительно есть картинка.
            try {
              out = await collectReadyBundle(it);
            } catch (readyError) {
              throw new Error(primaryError.message + "; " + readyError.message);
            }
          }
          saveBlob(out.blob, label + "-spine-images.zip");
          log("… превью: комплект " + label + " собран · PNG " + out.png +
              " · картинок " + out.pictures + " · файлов " + out.files, "dim");
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
      card.appendChild(info);
      card.appendChild(actions);
      frag.appendChild(card);
    }
    grid.appendChild(frag);
    pumpStills(batch);

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
    showPreviewsPanel(false, true);
    els.log.textContent = "";
    run.job = "";
    run.t0 = 0;
    run.step = "";
    run.logTimerLine = null;
    run.finalReady = false;
    const badge = $("run-badge");
    if (badge) badge.textContent = "in_progress";
    let job = null;
    const t0 = Date.now();
    showRunPanel(true, { title: "Запускаю проход…", step: "Отправляю задачу в GitHub Actions…" });
    try {
      if (url) {
        setStatus("скачиваем ассеты…", "run");
        setRunStep("Скачиваю ассеты игры и тяну скелеты, атласы и текстуры…");
        log("> Ссылка: " + url, "dim");
        log("> Загружаю манифест игры и тяну скелеты, атласы и текстуры…", "dim");
        job = await startConvertUrl(url);
        run.job = job;
        $("run-job").textContent = job;
        loadHistory();
        log("> Задача: " + job + " (режим: ссылка)", "dim");
        setStatus("скачиваем и конвертируем…", "run");
        setRunStep("Проход идёт в GitHub Actions: выкачивание, компиляция .spine и превью…");
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
        setRunStep("Загрузил файлы, жду компиляцию…");
        job = await startConvert(blob);
        run.job = job;
        $("run-job").textContent = job;
        loadHistory();
        log("> Задача: " + job, "dim");
        setStatus("конвертация…", "run");
      }
      const taskNames = url ? [] : skeletonNames();
      if (url) log("… идёт выкачивание и конвертация, следим за логом задачи…", "dim");
      // не ждём: карточки появятся сразу после обнаружения, параллельно с
      // компиляцией, а в конце перезальются готовыми .spine
      const rawCardsTask = url ? showRawCards(job, t0).catch(() => false) : Promise.resolve(false);
      await waitStatus(job, t0, taskNames);
      run.finalReady = true;
      await rawCardsTask;
      log("> Архив получен, проверяем результат…", "dim");
      lastZip = await fetchResult(job);
      if (url) log("… архив получен, проверяем состав результата", "dim");

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
      els.download.href = BROKER + "/download?job=" + encodeURIComponent(job);
      els.download.classList.remove("hidden");
      let hasPreviews = false;
      try {
        hasPreviews = await renderPreviews(lastZip);
      } catch (e) {
        log("Превью: " + e.message, "err");
      }
      if (hasPreviews) {
        setStatus("готово", "ok");
        markRunDone();
        log("… анимаций в сборке: " + $("previews-grid").children.length, "dim");
        setRunStep("Готово — анимации показаны ниже, нажми на блок чтобы скачать одну пару.");
      } else {
        let spineCount = 0;
        try {
          const archive = await JSZip.loadAsync(lastZip);
          spineCount = Object.keys(archive.files).filter((name) => /\.spine$/i.test(name) && !archive.files[name].dir).length;
        } catch (_) { /* ниже покажем отсутствие превью */ }
        markRunDone();
        if (spineCount === 0) {
          setStatus("нет анимаций", "err");
          log("Ошибка: архив получен, но конвертер не создал ни одного .spine-файла. Результат не считается успешным.", "err");
          showRunPanel(true, { title: "Анимации не созданы", step: "Конвертация завершилась без .spine-файлов. Архив доступен для диагностики." });
          const badge = $("run-badge");
          if (badge) badge.textContent = "ошибка";
        } else {
          setStatus("готово без превью", "ok");
          showRunPanel(true, { title: "Файлы созданы, но превью отсутствует", step: "Архив содержит .spine-файлы, однако карточки анимаций не сформированы." });
          const badge = $("run-badge");
          if (badge) badge.textContent = "без превью";
        }
        document.body.classList.remove("running");
        stopRunTicker();
      }
      loadHistory();
      loadStats();
    } catch (e) {
      log("Ошибка: " + e.message, "err");
      setStatus("ошибка", "err");
      document.body.classList.remove("running");
      stopRunTicker();
      const badge = $("run-badge");
      if (badge) badge.textContent = "ошибка";
      setRunStep("Проход не удался: " + e.message);
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
