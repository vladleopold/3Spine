// Тестовый стенд для 3Spine: собирает zip с настоящим Spine-web-JSON,
// атласом и видимой картинкой, поднимает сайт локально и проверяет в
// браузере, что карточки рисуют остановленный кадр и что наведение
// включает анимацию.
//
// Зависимостей нет: PNG собираем вручную, в браузер заходим по CDP
// (глобальный WebSocket из Node 22).
const { solidPng } = require("./preview_png.cjs");
const http = require("http");
const fs = require("fs");
const path = require("path");
const os = require("os");
const { spawn, spawnSync } = require("child_process");

const SITE = path.resolve(process.env.SITE_DIR || path.join(__dirname, "..", "frontend"));
const PORT = 8099;
const CDP = 9334;
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

// ─── Spine 3.8 web-JSON: одна кость, один слот с регионом, поворот ───
function skeletonJson() {
  return JSON.stringify({
    skeleton: { hash: "t", spine: "3.8.99", x: 0, y: 0, width: 64, height: 64, images: ["./"] },
    bones: [{ name: "root" }],
    slots: [{ name: "square", bone: "root", attachment: "box" }],
    skins: [{
      name: "default",
      attachments: {
        square: {
          box: { type: "region", path: "box", x: 0, y: 0, width: 64, height: 64,
                 rotation: 0, scaleX: 1, scaleY: 1, image: "box" },
        },
      },
    }],
    ik: [], transform: [], paths: [], events: [],
    animations: {
      spin: {
        bones: { root: { rotate: [{ time: 0, angle: 0, curve: [0.25, 0, 0.25, 1] },
                                  { time: 0.5, angle: 90 }] } },
      },
    },
  });
}

const atlasText = [
  "box.png",
  "size: 64,64",
  "format: RGBA8888",
  "filter: Nearest,Nearest",
  "repeat: none",
  "box",
  "  rotate: 0",
  "  xy: 0, 0",
  "  size: 64, 64",
  "  orig: 64, 64",
  "  offset: 0, 0",
  "  index: -1",
  "",
].join("\n");

// ─── zip без зависимостей: только stored, этого достаточно ───
function crc32(buf) {
  let c, t = [];
  for (let n = 0; n < 256; n++) { c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; }
  let crc = 0xffffffff;
  for (let i = 0; i < buf.length; i++) crc = t[(crc ^ buf[i]) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

function zipStore(files) {
  const parts = [], central = [];
  let offset = 0;
  for (const [name, data] of Object.entries(files)) {
    const nb = Buffer.from(name, "utf8");
    const crc = crc32(data);
    const lh = Buffer.alloc(30);
    lh.writeUInt32LE(0x04034b50, 0); lh.writeUInt16LE(20, 4); lh.writeUInt16LE(0, 6);
    lh.writeUInt16LE(0, 8); lh.writeUInt16LE(0, 10); lh.writeUInt16LE(0, 12);
    lh.writeUInt32LE(crc, 14); lh.writeUInt32LE(data.length, 18); lh.writeUInt32LE(data.length, 22);
    lh.writeUInt16LE(nb.length, 26); lh.writeUInt16LE(0, 28);
    parts.push(lh, nb, data);
    const ch = Buffer.alloc(46);
    ch.writeUInt32LE(0x02014b50, 0); ch.writeUInt16LE(20, 4); ch.writeUInt16LE(20, 6);
    ch.writeUInt16LE(0, 8); ch.writeUInt16LE(0, 10); ch.writeUInt16LE(0, 12); ch.writeUInt16LE(0, 14);
    ch.writeUInt32LE(crc, 16); ch.writeUInt32LE(data.length, 20); ch.writeUInt32LE(data.length, 24);
    ch.writeUInt16LE(nb.length, 28); ch.writeUInt16LE(0, 30); ch.writeUInt16LE(0, 32);
    ch.writeUInt16LE(0, 34); ch.writeUInt16LE(0, 36); ch.writeUInt32LE(0, 38);
    ch.writeUInt32LE(offset, 42);
    central.push(Buffer.concat([ch, nb]));
    offset += lh.length + nb.length + data.length;
  }
  const cd = Buffer.concat(central);
  const eo = Buffer.alloc(22);
  eo.writeUInt32LE(0x06054b50, 0);
  eo.writeUInt16LE(Object.keys(files).length, 8); eo.writeUInt16LE(Object.keys(files).length, 10);
  eo.writeUInt32LE(cd.length, 12); eo.writeUInt32LE(offset, 16);
  return Buffer.concat([...parts, cd, eo]);
}

// N анимаций сразу: стенд проверяет не одну карточку, а поведение при
// реальной нагрузке (в бою их 39).
function buildZip(n) {
  const COUNT = n || Number(process.env.CARDS || 1);
  const files = { "previews/index.json": null };
  const items = [];
  for (let k = 0; k < COUNT; k++) {
    const name = "anim" + k;
    const img = solidPng(64, 64, [255, 60 + k, 60, 255]);
    // Поле png оставляем: сборка его пишет, и наш фильтр по нему отсеивает.
    // Карточка его уже не рисует — в тесте проверяем именно это.
    items.push({ spine: "res/" + name + ".spine", web: "spine/" + name + "/" + name + ".json",
                 png: "previews/" + name + ".png",
                 name: name, bytes: img.length, kind: "render" });
    files["res/" + name + ".spine"] = Buffer.from("SPINE");
    files["spine/" + name + "/" + name + ".json"] = Buffer.from(skeletonJson(), "utf8");
    files["spine/" + name + "/" + name + ".atlas"] = Buffer.from(atlasText, "utf8");
    files["spine/" + name + "/box.png"] = img;
    files["previews/" + name + ".png"] = img;
  }
  files["previews/index.json"] = Buffer.from(JSON.stringify({ items: items }), "utf8");
  return zipStore(files);
}

// ─── статика сайта ───
const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
               ".json": "application/json", ".png": "image/png" };
function serve() {
  return new Promise((res) => {
    const s = http.createServer((req, rq) => {
      const u = decodeURIComponent(req.url.split("?")[0]);
      let f = path.join(SITE, u === "/" ? "index.html" : u);
      if (!f.startsWith(SITE) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) {
        rq.writeHead(404); return rq.end("no");
      }
      rq.writeHead(200, { "content-type": MIME[path.extname(f)] || "application/octet-stream" });
      rq.end(fs.readFileSync(f));
    });
    s.listen(PORT, () => res(s));
  });
}

// ─── CDP ───
function httpGetJson(url) {
  return new Promise((res, rej) => {
    const req = http.get(url, (r) => {
      let b = ""; r.on("data", (c) => (b += c)); r.on("end", () => { try { res(JSON.parse(b)); } catch (e) { rej(e); } });
    });
    req.setTimeout(5000, () => req.destroy(new Error("timeout")));
    req.on("error", rej);
  });
}

function cdp(ws) {
  return new Promise((res, rej) => {
    const s = new WebSocket(ws); let id = 0; const pend = new Map();
    const listeners = {};
    s.addEventListener("open", () => res({
      send: (method, params) => new Promise((ok, no) => {
        const i = ++id; pend.set(i, { ok, no });
        s.send(JSON.stringify({ id: i, method, params: params || {} }));
        setTimeout(() => { if (pend.has(i)) { pend.delete(i); no(new Error("cdp timeout " + method)); } }, 30000);
      }),
      on: (ev, fn) => { (listeners[ev] = listeners[ev] || []).push(fn); },
      close: () => s.close(),
    }));
    s.addEventListener("error", rej);
    s.addEventListener("message", (e) => {
      const m = JSON.parse(e.data);
      if (m.id && pend.has(m.id)) {
        const p = pend.get(m.id); pend.delete(m.id);
        m.error ? p.no(new Error(JSON.stringify(m.error))) : p.ok(m.result);
      } else if (m.method) {
        const ls = listeners[m.method] || [];
        // Всем остальным событиям отдаём params как есть: иначе, скажем,
        // Fetch.requestPaused до слушателя не дойдёт и перехват молчит.
        if (ls.length) ls.forEach((f) => f(m.params || {}));
      }
    });
  });
}

async function evalPage(c, expr) {
  const r = await c.send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error("page: " + (r.exceptionDetails.exception || {}).description);
  return r.result.value;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fails = [];
const ok = (cond, name, extra) => {
  console.log((cond ? "  OK   " : "  FAIL ") + name + (extra ? "  → " + extra : ""));
  if (!cond) fails.push(name);
};

(async () => {
  const server = await serve();
  console.log("сайт на http://127.0.0.1:" + PORT);

  const prof = path.join(os.tmpdir(), "3spine-preview-prof");
  try { fs.rmSync(prof, { recursive: true, force: true, maxRetries: 3 }); } catch (e) { /* хвост прошлого Chrome */ }
  const chrome = spawn(CHROME, [
    "--headless=new", "--remote-debugging-port=" + CDP, "--user-data-dir=" + prof,
    "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "about:blank",
  ], { stdio: "ignore", detached: false });

  // ждём CDP
  let ver = null;
  for (let i = 0; i < 40 && !ver; i++) { await sleep(300); try { ver = await httpGetJson("http://127.0.0.1:" + CDP + "/json/version"); } catch (e) {} }
  if (!ver) throw new Error("CDP не поднялся");
  console.log("браузер:", ver.Browser);

  const targets = await httpGetJson("http://127.0.0.1:" + CDP + "/json/list");
  const page = targets.find((t) => t.type === "page");
  const c = await cdp(page.webSocketDebuggerUrl);
  await c.send("Page.enable");
  await c.send("Runtime.enable");

  const errors = [];
  c.send("Runtime.enable").catch(() => {});
  c.on("exception", (m) => errors.push("exception: " + (m.text || "")));
  c.on("console", (m) => { if (m.type === "error") errors.push(m.text || "console error"); });
  const sink = setInterval(() => {}, 1000);

  // Перехват брокера включаем ДО загрузки страницы: история тянется
  // сразу при старте, и перехватить её поздно — её не будет в DOM.
  // СНАЧАЛА собираем архив, ПОТОМ читаем его в base64 — наоборот читается
  // прошлый файл, и проверка молча проверяет старое число карточек.
  const zipPath = path.join(os.tmpdir(), "3spine-preview-test.zip");
  fs.writeFileSync(zipPath, buildZip());
  const zipB64Early = fs.readFileSync(zipPath).toString("base64");
  await c.send("Fetch.enable", { patterns: [{ urlPattern: "*spine-broker*" }] });
  const handlerEarly = (e) => {
    const url = String(e.request && e.request.url);
    const rid = e.requestId;
    if (url.indexOf("/download") >= 0) {
      console.log("   >> fulfill /download, base64 len=" + String(zipB64Early).length);
      c.send("Fetch.fulfillRequest", { requestId: rid, responseCode: 200,
        responseHeaders: [{ name: "content-type", value: "application/zip" },
                           { name: "access-control-allow-origin", value: "*" }],
        body: zipB64Early })
        .catch((e) => console.log("   !! fulfill /download не удался:", e.message));
    } else if (url.indexOf("/history") >= 0) {
      const hist = { items: [{ job: "test-job", file: "test.zip",
        date: "2026-09-30T08:00:00Z", bytes: 1234, spine: true }] };
      const hb = Buffer.from(JSON.stringify(hist)).toString("base64");
      c.send("Fetch.fulfillRequest", { requestId: rid, responseCode: 200,
        responseHeaders: [{ name: "content-type", value: "application/json" },
                           { name: "access-control-allow-origin", value: "*" }],
        body: hb }).then(() => console.log("   >> отдан /history, base64 len=" + hb.length))
        .catch((e) => console.log("   !! fulfill /history не удался:", e.message));
    } else if (url.indexOf("/stats") >= 0 || url.indexOf("/visit") >= 0) {
      c.send("Fetch.fulfillRequest", { requestId: rid, responseCode: 200,
        responseHeaders: [{ name: "content-type", value: "application/json" }],
        body: Buffer.from(JSON.stringify({ visits: 1, ok: true })).toString("base64") }).catch(() => {});
    } else {
      c.send("Fetch.continueRequest", { requestId: rid }).catch(() => {});
    }
  };
  c.on("Fetch.requestPaused", (e) => {
    console.log("   >> перехват:", String(e.request && e.request.url).slice(0, 90));
    handlerEarly(e);
  });

  await c.send("Page.navigate", { url: "http://127.0.0.1:" + PORT + "/" });
  await sleep(1500);

  // Подсовываем собранный zip через настоящий путь приложения —
// перехватываем брокер и отдаём наш архив на /download.
// input.files нельзя надёжно присвоить из JS, поэтому идём по сети,
// как это делает openJobArchive() при открытии сохранённого прохода.
console.log("\n1. Открываем панель превью с тестовым архивом");

  const probe = await evalPage(c, `(async () => {
    const r = await fetch("https://spine-broker.leopolds2010.workers.dev/history");
    const t = await r.text();
    const items = document.querySelectorAll("#history-list .history-item");
    return JSON.stringify({
      http: r.status, body: t.slice(0, 200),
      historyItems: items.length,
      listHTML: (document.querySelector("#history-list").innerHTML || "").slice(0, 220),
    });
  })()`);
  console.log("   прямой запрос:", probe);
  const opened = await evalPage(c, `(async () => {
    const items = document.querySelectorAll("#history-list .history-item");
    if (!items.length) return "нет проходов в истории";
    items[0].click();
    return "клик по проходу";
  })()`);
  ok(/клик/.test(String(opened)), "проход открыт", String(opened));

  await sleep(Number(process.env.WAIT_MS || 4000));

  const diag = await evalPage(c, `(() => {
    const log = document.querySelector("#log");
    const panel = document.querySelector("#previews-panel");
    const grid = document.querySelector("#previews-grid");
    return {
      log: (log && log.textContent || "").slice(-700),
      panelDisplay: panel ? getComputedStyle(panel).display : "нет панели",
      panelHidden: panel ? panel.className : "-",
      gridChildren: grid ? grid.children.length : -1,
      status: (document.querySelector("#run-status") || {}).textContent || "",
      stillApi: typeof (window.SpineCardPlayer && window.SpineCardPlayer.still),
    };
  })()`);
  console.log("   диагностика:", JSON.stringify(diag, null, 1));

  console.log("\n2. Карточка отрисовывает остановленный кадр");
  const state = await evalPage(c, `(() => {
    const card = document.querySelector("#previews-grid .pv-card");
    if (!card) return { err: "карточек нет" };
    const shot = card.querySelector(".pv-shot");
    const cv = shot && shot.querySelector("canvas.pv-still");
    const img = shot && shot.querySelector("img");
    let nonEmpty = null;
    if (cv) {
      try {
        const g = cv.getContext("2d");
        const d = g.getImageData(0, 0, cv.width, cv.height).data;
        let n = 0;
        for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++;
        nonEmpty = { w: cv.width, h: cv.height, opaque: n };
      } catch (e) { nonEmpty = { err: String(e) }; }
    }
    return {
      cards: document.querySelectorAll("#previews-grid .pv-card").length,
      panelVisible: !!document.querySelector("#previews-panel") &&
        getComputedStyle(document.querySelector("#previews-panel")).display !== "none",
      hasCanvas: !!cv,
      hasImg: !!img,
      canvasInfo: nonEmpty,
      note: (card.querySelector(".pv-note") || {}).textContent || "",
      animDisabled: (card.querySelector(".pv-anim") || {}).disabled,
      animOptions: (card.querySelector(".pv-anim") || {}).options
        ? card.querySelector(".pv-anim").options.length : 0,
      stillApi: typeof (window.SpineCardPlayer && window.SpineCardPlayer.still),
      errs: (window.__errs || []).slice(0, 5),
    };
  })()`);

  const want = Number(process.env.CARDS || 1);
  ok(state.cards === want, "карточек нарисовано " + want + " (получено " + state.cards + ")", JSON.stringify(state.cards));
  ok(!state.hasImg, "скриншота-картинки в карточке нет");
  ok(state.hasCanvas, "есть canvas.pv-still с кадром анимации");
  ok(state.canvasInfo && state.canvasInfo.opaque > 0, "кадр НЕ пустой",
     state.canvasInfo ? JSON.stringify(state.canvasInfo) : "нет canvas");
  // У тестового скелета одна анимация "spin", поэтому 1 опция — это
  // верный результат: плейспейсдер «наведи на карточку» убран, реальная
  // анимация подставлена.
  ok(state.stillApi === "function", "плеер умеет рисовать остановленный кадр (still)");

  console.log("\n2b. ВСЕ карточки держат непустой кадр (нагрузка)");
  const all = await evalPage(c, `(() => {
    const cards = Array.from(document.querySelectorAll("#previews-grid .pv-card"));
    let withCanvas = 0, opaqueTotal = 0, empty = 0;
    for (const c of cards) {
      const cv = c.querySelector("canvas.pv-still");
      if (!cv) { empty++; continue; }
      withCanvas++;
      try {
        const d = cv.getContext("2d").getImageData(0, 0, cv.width, cv.height).data;
        let n = 0;
        for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++;
        if (n > 0) opaqueTotal++;
      } catch (e) { /* webgl без getImageData */ }
    }
    return { total: cards.length, withCanvas, painted: opaqueTotal, empty };
  })()`);
  ok(all.total === want, "карточек в сетке: " + want, JSON.stringify(all));
  ok(all.withCanvas === want, "у каждой карточки свой канвас", JSON.stringify(all));
  ok(all.painted === want, "у каждой карточки НЕПУСТОЙ кадр", JSON.stringify(all));

  console.log("\n3. Наведение включает анимацию (кадр меняется)");
  // Замороженный кадр лежит в canvas.pv-still; при наведении плеер
  // добавляет рядом свой canvas.pv-live. Читаем живой.
  const before = await evalPage(c, `(() => {
    const shot = document.querySelector("#previews-grid .pv-card .pv-shot");
    const cv = shot && shot.querySelector("canvas");
    return cv ? cv.toDataURL().length : 0;
  })()`);
  await evalPage(c, `(() => {
    const card = document.querySelector("#previews-grid .pv-card");
    card.dispatchEvent(new MouseEvent("mouseenter", { bubbles: false }));
    return 1;
  })()`);
  await sleep(1200);
  const during = await evalPage(c, `(async () => {
    const shot = document.querySelector("#previews-grid .pv-card .pv-shot");
    const cv = shot && shot.querySelector("canvas.pv-live");
    if (!cv) return { animating: false, len: 0, reason: "нет pv-live" };
    const a = cv.toDataURL();
    await new Promise(r => setTimeout(r, 250));
    const b = cv.toDataURL();
    return { animating: a !== b, len: a.length };
  })()`);
  ok(during.animating === true, "на mouseenter кадр перерисовывается (анимация играет)",
     JSON.stringify(during));
  ok(before !== during.len, "кадр отличается от замороженного");

  // Список анимаций наполняется при наведении (плеер отдаёт имена из
  // загруженного скелета), поэтому проверяем его уже после mouseenter.
  const animState = await evalPage(c, `(() => {
    const sel = document.querySelector("#previews-grid .pv-card select.pv-anim");
    if (!sel) return { n: -1, disabled: null };
    return { n: sel.options.length, disabled: sel.disabled };
  })()`);
  ok(animState.n === 1 && animState.disabled === false,
     "список анимаций заполнен после наведения (" + animState.n + " опция, select разблокирован)",
     JSON.stringify(animState));

  console.log("\n4. Уход курсора снова замораживает кадр");
  await evalPage(c, `(() => {
    const card = document.querySelector("#previews-grid .pv-card");
    card.dispatchEvent(new MouseEvent("mouseleave", { bubbles: false }));
    return 1;
  })()`);
  await sleep(600);
  const after = await evalPage(c, `(async () => {
    const cv = document.querySelector("#previews-grid .pv-card canvas.pv-still");
    if (!cv) return { gone: true };
    const a = cv.toDataURL();
    await new Promise(r => setTimeout(r, 400));
    return { gone: false, still: a === cv.toDataURL() };
  })()`);
  ok(!after.gone && after.still === true, "после mouseleave кадр остаётся на месте и не играет",
     JSON.stringify(after));

  // Скриншот сетки — глазами видно, что кадры настоящие.
  try {
    const shot = await c.send("Page.captureScreenshot", { format: "png" });
    const gridShot = path.join(os.tmpdir(), "3spine-preview-grid.png");
    fs.writeFileSync(gridShot, Buffer.from(shot.data, "base64"));
    console.log("   снимок сетки: " + gridShot);
  } catch (e) { console.log("   снимок не снялся:", e.message); }

  console.log("\n5. Ошибок в консоли нет");
  const consoleErrs = errors.slice(0, 5);
  ok(consoleErrs.length === 0, "консоль чистая", consoleErrs.join(" | "));

  c.close();
  clearInterval(sink);
  chrome.kill("SIGTERM");
  server.close();
  try { fs.rmSync(prof, { recursive: true, force: true, maxRetries: 3 }); } catch (e) { /* Chrome ещё держит папку — не страшно */ }
  console.log(fails.length ? "\nПРОВАЛЕНО: " + fails.length + " → " + fails.join("; ")
                            : "\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ");
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.error("ОШИБКА:", e.message); process.exit(2); });