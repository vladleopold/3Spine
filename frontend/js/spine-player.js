/* Нативный проигрыватель Spine в карточках превью.
 *
 * Рантаймы лежат локально в frontend/vendor — сайт не ходит за ними в интернет.
 * Поддерживаются линии 3.5, 3.6, 3.7, 3.8, 4.0, 4.1, 4.2, 4.3: версия выбирается
 * из поля skeleton.spine в JSON скелета, рантайм грузится один раз и переиспользуется.
 * Одновременно живёт только один WebGL-контекст и один canvas: карточка под курсором
 * одна, а лимит браузеров на GL-контексты лучше не расходовать на все 39 карточек.
 * Если что-то не получилось — карточка молча остаётся со статичной картинкой.
 */
(function () {
  "use strict";

  var RUNTIMES = {
    "3.5": "vendor/spine-webgl-3.5.js",
    "3.6": "vendor/spine-webgl-3.6.js",
    "3.7": "vendor/spine-webgl-3.7.js",
    "3.8": "vendor/spine-webgl-3.8.js",
    "4.0": "vendor/spine-webgl-4.0.31.js",
    "4.1": "vendor/spine-webgl-4.1.55.js",
    "4.2": "vendor/spine-webgl-4.2.120.js",
    "4.3": "vendor/spine-webgl-4.3.13.js"
  };
  var ORDER = ["3.5", "3.6", "3.7", "3.8", "4.0", "4.1", "4.2", "4.3"];

  var nsCache = {};      // ключ рантайма -> его namespace spine
  var loadQueue = {};    // ключ рантайма -> промис загрузки скрипта

  // «3.8.99» -> «3.8»; если такой линии нет, берём ближайшую старшую в семействе,
  // а если версия новее всех — самый свежий из имеющихся.
  function pickRuntime(version) {
    var m = /^\s*(\d+)\.(\d+)/.exec(String(version || ""));
    if (!m) return "3.8";
    var want = m[1] + "." + m[2];
    if (RUNTIMES[want]) return want;
    for (var i = 0; i < ORDER.length; i++) {
      if (cmp(ORDER[i], want) >= 0) return ORDER[i];
    }
    return ORDER[ORDER.length - 1];
  }

  function cmp(a, b) {
    var pa = a.split("."), pb = b.split(".");
    for (var i = 0; i < 2; i++) {
      var d = (+pa[i] || 0) - (+pb[i] || 0);
      if (d) return d;
    }
    return 0;
  }

  function loadRuntime(key) {
    if (nsCache[key]) return Promise.resolve(nsCache[key]);
    if (loadQueue[key]) return loadQueue[key];
    loadQueue[key] = new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = RUNTIMES[key];
      s.onload = function () {
        var ns = window.spine;
        if (!ns || !ns.webgl) { reject(new Error("рантайм " + key + " без webgl")); return; }
        nsCache[key] = ns;
        resolve(ns);
      };
      s.onerror = function () { reject(new Error("не скачался рантайм Spine " + key)); };
      document.head.appendChild(s);
    }).catch(function (e) { delete loadQueue[key]; throw e; });
    return loadQueue[key];
  }

  var st = {
    host: null,     // .pv-shot, в котором сейчас играет анимация
    canvas: null,
    gl: null,
    skeleton: null,
    renderer: null,
    track: null,
    state: null,     // AnimationState
    anims: null,
    nsKey: "",      // какой рантайм сейчас задействован
    image: null,    // HTMLImageElement с листом атласа
    raf: 0,
    dirty: true
  };

  function dropCanvas() {
    if (st.raf) { cancelAnimationFrame(st.raf); st.raf = 0; }
    if (st.canvas && st.canvas.parentNode) st.canvas.parentNode.removeChild(st.canvas);
    st.canvas = null;
    st.gl = null;
    st.skeleton = null;
    st.renderer = null;
    st.track = null;
    st.state = null;
  }

  function makeCanvas() {
    var c = document.createElement("canvas");
    c.width = 512;
    c.height = 512;
    c.className = "pv-live";
    var gl = c.getContext("webgl", { alpha: true, antialias: true, premultipliedAlpha: true, preserveDrawingBuffer: true }) ||
             c.getContext("experimental-webgl");
    if (!gl) return null;
    st.canvas = c;
    st.gl = gl;
    return c;
  }

  function loadImage(blob) {
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(blob);
      var img = new Image();
      img.onload = function () { resolve({ img: img, url: url }); };
      img.onerror = function () { URL.revokeObjectURL(url); reject(new Error("не читается лист атласа")); };
      img.src = url;
    });
  }

  // Собираем скелет. У всех восьми сборок один и тот же современный API:
  // рендерер принимает WebGL-контекст, а не canvas, и своего GLContext класса
  // в сборке нет — чистим буфер сами через gl. В 3.x классы лежат в spine.webgl,
  // в 4.x — прямо в spine, поэтому берём по наличию.
  function build(ns, key, jsonText, atlasText, image) {
    var w = ns.webgl || ns;
    var data = JSON.parse(jsonText);
    var gl = st.gl;
    var TexClass = ns.GLTexture || w.GLTexture || ns.Texture;   // в 3.x GLTexture живёт в spine.webgl
    var glTex = new TexClass(gl, image);
    if (typeof glTex.load === "function") glTex.load();
    else if (typeof glTex.upload === "function") glTex.upload();
    // TextureAtlas ждёт функцию-загрузчик листа, а не сам Texture
    var atlas = new ns.TextureAtlas(atlasText, function () { return glTex; });
    // SkeletonJson во всех этих сборках принимает ровно один аргумент и ждёт
    // загрузчик вложений; если дать ему атлас, разбор падает на первом регионе
    var loader = new ns.AtlasAttachmentLoader(atlas);
    var sd = new ns.SkeletonJson(loader).readSkeletonData(data);
    var skeleton = new ns.Skeleton(sd);
    st.skeleton = skeleton;
    st.renderer = new w.SkeletonRenderer(gl);
    st.v4 = key.charAt(0) === "4";
    // в этих сборках у Skeleton нет setAnimation: анимации ведутся через AnimationState
    st.state = ns.AnimationState ? new ns.AnimationState(new ns.AnimationStateData(sd)) : null;
    st.anims = sd.animations || [];
    return skeleton;
  }

  function fit(skeleton) {
    var b = skeleton.data.bounds;
    if (!b || b.width <= 0 || b.height <= 0) return;
    var pad = 8;
    var scale = Math.min((st.canvas.width - pad * 2) / b.width, (st.canvas.height - pad * 2) / b.height);
    if (!isFinite(scale) || scale <= 0) return;
    skeleton.scaleX = scale;
    skeleton.scaleY = scale;
    skeleton.x = (st.canvas.width - b.width * scale) / 2 - b.x * scale;
    skeleton.y = (st.canvas.height - b.height * scale) / 2 - b.y * scale;
  }

  function draw() {
    var sk = st.skeleton;
    if (!sk) return;
    if (st.state) st.state.apply(sk);
    sk.update(st.dt || 0.016);
    st.dt = 0.016;
    var gl = st.gl;
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    st.renderer.draw(sk);
  }

  function loop(now) {
    st.raf = 0;
    if (!st.host || !st.skeleton) return;
    st.dt = Math.min(0.05, st.last ? (now - st.last) / 1000 : 0.016);
    st.last = now;
    draw();
    st.raf = requestAnimationFrame(loop);
  }

  // assets: { json: string, atlas: string, page: Blob }
  function play(host, assets) {
    stop();
    st.host = host;
    Promise.resolve(assets).then(function (a) {
      if (!a || st.host !== host) return;      // курсор уже ушёл
      var key = pickRuntime(readVersion(a.json));
      return loadRuntime(key).then(function (ns) {
        if (st.host !== host) return;
        if (st.nsKey !== key) { dropCanvas(); st.nsKey = key; }
        if (!st.canvas && !makeCanvas()) throw new Error("нет WebGL");
        return loadImage(a.page).then(function (r) {
          if (st.host !== host) return;
          st.image = r.img;
          var sk = build(ns, key, a.json, a.atlas, r.img);
          fit(sk);
          sk.setToSetupPose();
          var anims = st.anims && st.anims.length ? st.anims : sk.data.animations;
          if (st.state && anims && anims.length) {
            st.state.setAnimation(0, anims[0].name, true);
          } else if (sk.setAnimation && anims && anims.length) {
            st.track = sk.setAnimation(0, anims[0].name, true);   // старый API, если вдруг
          }
          host.appendChild(st.canvas);
          st.last = 0;
          st.raf = requestAnimationFrame(loop);
        });
      });
    }).catch(function (e) {
      // не получилось — карточка остаётся со статичным превью
      if (window.console && console.warn) console.warn("spine-player:", (e && e.message) || e);
      if (st.host === host) stop();
    });
  }

  function stop() {
    st.host = null;
    st.last = 0;
    dropCanvas();
  }

  function readVersion(jsonText) {
    try {
      var d = JSON.parse(jsonText);
      if (d && d.skeleton && d.skeleton.spine) return d.skeleton.spine;
    } catch (e) { /* версию не угадали — возьмём 3.8 по умолчанию */ }
    return "3.8";
  }

  window.SpineCardPlayer = { play: play, stop: stop, version: readVersion };
})();
