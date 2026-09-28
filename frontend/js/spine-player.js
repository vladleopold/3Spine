/* Нативный проигрыватель Spine в карточках превью.
 *
 * Рантаймы лежат локально в frontend/vendor — сайт не ходит за ними в интернет.
 * Версия скелета читается из skeleton.spine, под неё подбирается сборка.
 * Основной путь — 2D-канвас: он не ест WebGL-контексты (их у браузера штук 16
 * на всю страницу, а карточек бывает 39) и рисует в одном общем canvas.
 * Если рантайм не справился или первый кадр пустой — карточка остаётся
 * со статичным превью, хуже не становится.
 */
(function () {
  "use strict";

  var RUNTIMES = {
    "3.5": ["vendor/spine-canvas-3.5.js", "vendor/spine-webgl-3.5.js"],
    "3.6": ["vendor/spine-canvas-3.6.js", "vendor/spine-webgl-3.6.js"],
    "3.7": ["vendor/spine-canvas-3.7.js", "vendor/spine-webgl-3.7.js"],
    "3.8": ["vendor/spine-canvas-3.8.js", "vendor/spine-webgl-3.8.js"],
    "4.0": ["vendor/spine-canvas-4.0.31.js"],
    "4.1": ["vendor/spine-canvas-4.1.55.js"],
    "4.2": ["vendor/spine-canvas-4.2.120.js"],
    "4.3": ["vendor/spine-canvas-4.3.13.js"]
  };
  var ORDER = ["3.5", "3.6", "3.7", "3.8", "4.0", "4.1", "4.2", "4.3"];

  var nsCache = {};      // ключ -> namespace spine
  var loadQueue = {};    // ключ -> промис загрузки скрипта

  // «3.8.99» -> «3.8»; если такой линии нет, берём ближайшую старшую,
  // а если версия новее всех — самую свежую из имеющихся.
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

  function loadRuntime(url) {
    if (loadQueue[url]) return loadQueue[url];
    loadQueue[url] = new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = url;
      s.onload = function () { resolve(window.spine); };
      s.onerror = function () { reject(new Error("не скачался " + url)); };
      document.head.appendChild(s);
    });
    loadQueue[url] = loadQueue[url].catch(function (e) { delete loadQueue[url]; throw e; });
    return loadQueue[url];
  }

  var st = {
    host: null,     // .pv-shot, в котором сейчас играет анимация
    canvas: null,
    g2: null,
    skeleton: null,
    state: null,    // AnimationState
    renderer: null,
    anims: null,
    raf: 0,
    last: 0,
    dt: 0.016,
    cssW: 0,
    cssH: 0,
    current: 0,       // индекс играемой анимации
    checked: false  // первый кадр проверили — дальше не тратим время на чтение пикселей
  };

  function drop() {
    if (st.raf) { cancelAnimationFrame(st.raf); st.raf = 0; }
    if (st.canvas && st.canvas.parentNode) st.canvas.parentNode.removeChild(st.canvas);
    st.canvas = null;
    st.g2 = null;
    st.skeleton = null;
    st.state = null;
    st.renderer = null;
    st.saw = false;
    st.frames = 0;
  }

  // Канвас-рендереры 4.x рисуют в 2D-контексте и считают координаты в CSS-пикселях,
  // поэтому рисуем с запасом под devicePixelRatio, иначе картинка мылится.
  function sizeCanvas() {
    var rect = st.host.getBoundingClientRect();
    var w = Math.max(64, Math.round(rect.width) || 300);
    var h = Math.max(64, Math.round(rect.height) || 150);
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    st.canvas.width = Math.round(w * dpr);
    st.canvas.height = Math.round(h * dpr);
    st.cssW = w;
    st.cssH = h;
    st.g2 = st.canvas.getContext("2d");
    st.g2.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function loadImage(blob) {
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(blob);
      var img = new Image();
      img.onload = function () { resolve(img); };
      img.onerror = function () { URL.revokeObjectURL(url); reject(new Error("не читается лист атласа")); };
      img.src = url;
    });
  }

  // Канвас-API: TextureAtlas(atlasText) без загрузчика, текстуры вешаются
  // на страницы через page.setTexture(), а вложения грузит AtlasAttachmentLoader.
  // Атлас Playson разложен не по спецификации: пустая строка стоит в начале
  // файла, а между свойствами страницы и первым регионом её нет. Официальный
  // парсер из-за этого читает ноль регионов и картинка не рисуется. Приводим
  // текст к каноничному виду: страница -> свойства -> пустая строка -> регионы.
  var PAGE_PROP = /^(size|format|filter|repeat):/;

  function normalizeAtlas(text) {
    var lines = String(text).replace(/\r\n/g, "\n").split("\n");
    var out = [];
    var i = 0;
    function blank() { return !lines[i] || !lines[i].trim(); }
    while (blank()) i++;
    while (i < lines.length) {
      out.push(lines[i]);                        // имя страницы
      i++;
      var props = [];
      while (i < lines.length && PAGE_PROP.test(lines[i].trim()) && !/^\s/.test(lines[i])) {
        props.push(lines[i]);
        i++;
      }
      if (props.length) {
        out.push("");
        props.forEach(function (p) { out.push(p); });
        out.push("");                            // разделитель страницы и регионов
      }
      while (i < lines.length && !blank()) {     // блок регионов
        out.push(lines[i]);
        i++;
      }
      while (blank()) i++;
    }
    return out.join("\n") + "\n";
  }

  function buildCanvas(ns, assets, images) {
    if (!ns.SkeletonRenderer || !ns.CanvasTexture) throw new Error("в сборке нет канвас-рендерера");
    var atlas = new ns.TextureAtlas(normalizeAtlas(assets.atlas));
    var pages = atlas.pages || [];
    if (pages.length > images.length) throw new Error("в атласе " + pages.length + " листов, а есть " + images.length);
    pages.forEach(function (page, i) { page.setTexture(new ns.CanvasTexture(images[i])); });
    var loader = new ns.AtlasAttachmentLoader(atlas);
    var sd = new ns.SkeletonJson(loader).readSkeletonData(JSON.parse(assets.json));
    var sk = new ns.Skeleton(sd);
    sk.setToSetupPose();
    sk.setSkin(0);
    sk.setSlotsToSetupPose();
    st.skeleton = sk;
    st.anims = sd.animations || [];
    st.pages = pages.length;
    st.regions = (function () {
      var n = 0;
      pages.forEach(function (p) { n += (p.regions || []).length; });
      return n;
    })();
    st.textured = (function () {
      var ok = 0, all = 0;
      pages.forEach(function (p) { (p.regions || []).forEach(function (r) {
        all++; if (r.texture) ok++;
      }); });
      return ok + "/" + all;
    })();
    st.slots = (sd.slots || []).length;
    st.attached = sk.slots.filter(function (s) { return !!s.attachment; }).length;
    st.imgs = images.map(function (i) { return i.naturalWidth + "x" + i.naturalHeight; }).join(",");
    st.bounds = (function () {
      var x = sk.data.x, y = sk.data.y, w = sk.data.width, h = sk.data.height;
      return w + "x" + h + "@" + x + "," + y;
    })();
    st.state = ns.AnimationState ? new ns.AnimationState(new ns.AnimationStateData(sd)) : null;
    st.renderer = new ns.SkeletonRenderer(st.g2);
    return sk;
  }

  // Запасной путь для сборок 3.x, где канвас-рендерера нет: WebGL.
  function buildWebgl(ns, assets, images) {
    var w = ns.webgl || ns;
    if (!w.SkeletonRenderer) throw new Error("в сборке нет webgl-рендерера");
    var Tex = ns.GLTexture || w.GLTexture;
    if (!Tex) throw new Error("в сборке нет GLTexture");
    var Managed = ns.ManagedWebGLRenderingContext || w.ManagedWebGLRenderingContext;
    var Scene = ns.SceneRenderer || w.SceneRenderer;
    if (!Managed || !Scene) throw new Error("в сборке нет webgl-сцены");
    var glc = new Managed(st.canvas);
    var gl = glc.gl;
    var tex = new Tex(gl, images[0]);
    if (typeof tex.update === "function") tex.update();
    else if (typeof tex.load === "function") tex.load();
    var atlas = new ns.TextureAtlas(assets.atlas, function () { return tex; });
    var sd = new ns.SkeletonJson(new ns.AtlasAttachmentLoader(atlas)).readSkeletonData(JSON.parse(assets.json));
    var sk = new ns.Skeleton(sd);
    sk.setToSetupPose();
    sk.setSkin(0);
    sk.setSlotsToSetupPose();
    st.skeleton = sk;
    st.anims = sd.animations || [];
    st.state = ns.AnimationState ? new ns.AnimationState(new ns.AnimationStateData(sd)) : null;
    st.scene = new Scene(st.canvas, glc, true);
    st.renderer = { draw: function (s) { st.scene.begin(); st.scene.drawSkeleton(s); st.scene.end(); } };
    return sk;
  }

  function fit(skeleton) {
    var b = skeleton.data.bounds;
    if (!b || !(b.width > 0) || !(b.height > 0)) return;
    var pad = 10;
    var w = st.cssW || st.canvas.width, h = st.cssH || st.canvas.height;
    var scale = Math.min((w - pad * 2) / b.width, (h - pad * 2) / b.height);
    if (!isFinite(scale) || scale <= 0) return;
    skeleton.scaleX = scale;
    skeleton.scaleY = scale;
    skeleton.x = (w - b.width * scale) / 2 - b.x * scale;
    skeleton.y = (h - b.height * scale) / 2 - b.y * scale;
  }

  // Пустой кадр — значит рантайм не тянет эти данные: лучше статичное
  // превью, чем пустая карточка под курсором. Проверять надо не на первом
  // кадре, а после прогрева: у многих анимаций (00_start, activation)
  // первый кадр пуст по определению — они появляются из прозрачности.
  function frameEmpty() {
    try {
      var d = st.g2.getImageData(0, 0, st.canvas.width, st.canvas.height).data;
      for (var i = 3; i < d.length; i += 4 * 29) {
        if (d[i] > 8) return false;
      }
      return true;
    } catch (e) {
      return false;                       // не смогли прочитать — не мешаем
    }
  }

  var WARMUP = 45;                        // кадров до первого вывода

  function tooEmpty() {
    if (st.saw) return false;             // хоть раз что-то нарисовалось
    st.frames = (st.frames || 0) + 1;
    if (st.frames < WARMUP) return false;
    if (!frameEmpty()) { st.saw = true; return false; }
    // пусто на всём прогреве — анимация в этих данных не играется
    if (st.frames < WARMUP + 90) return false;
    return true;
  }

  function loop(now) {
    st.raf = 0;
    if (!st.host || !st.skeleton) return;
    st.dt = Math.min(0.05, st.last ? (now - st.last) / 1000 : 0.016);
    st.last = now;
    // apply() только применяет позу; время двигает update()
    if (st.state) { st.state.update(st.dt); st.state.apply(st.skeleton); }
    st.skeleton.update(st.dt);
    st.skeleton.updateWorldTransform(0);
    st.g2.clearRect(0, 0, st.cssW || st.canvas.width, st.cssH || st.canvas.height);
    st.renderer.draw(st.skeleton);
    if (tooEmpty()) { stop(); return; }
    st.raf = requestAnimationFrame(loop);
  }

  // assets: { json, atlas, pages: [Blob, ...] }
  function play(host, assetsPromise, onReady) {
    stop();
    st.host = host;
    Promise.resolve(assetsPromise).then(function (assets) {
      if (!assets || st.host !== host) return;
      var key = pickRuntime(readVersion(assets.json));
      return Promise.all((assets.pages || []).map(loadImage)).then(function (images) {
        if (st.host !== host) return;
        if (!images.length) throw new Error("нет листов атласа");
        // canvas должен быть в DOM с размерами: SceneRenderer/рендерер
        // берут размеры из clientWidth, иначе вьюпорт нулевой
        var canvas = document.createElement("canvas");
        canvas.className = "pv-live";
        host.appendChild(canvas);
        st.canvas = canvas;
        sizeCanvas();
        // 3.x-сборки у Esoteric неполные (в canvas нет рендерера, в webgl
        // нечем рисовать), а 4.0-4.2 таймлайны 3.8 читают. Поэтому для 3.x
        // пробуем свою сборку, затем 4.0 -> 4.1 -> 4.2. 4.3 не подходит:
        // он таймлайны 3.8 не понимает.
        var list = (RUNTIMES[key] || []).slice();
        if (key.charAt(0) === "3") {
          list = list.concat(RUNTIMES["4.0"], RUNTIMES["4.1"], RUNTIMES["4.2"]);
        }
        var lastErr = null;
        return list.reduce(function (chain, url) {
          return chain.then(function (done) {
            if (done) return true;
            return loadRuntime(url).then(function (ns) {
              if (!ns || !ns.Skeleton) throw new Error("пустая сборка");
              try {
                st.scene = null;
                var sk = (ns.SkeletonRenderer && ns.CanvasTexture)
                  ? buildCanvas(ns, assets, images)
                  : buildWebgl(ns, assets, images);
                fit(sk);
                st.current = 0;
                if (st.state && st.anims.length) {
                  st.state.setAnimation(0, st.anims[0].name, true);
                }
                if (onReady) {
                  try { onReady(st.anims.map(function (a) { return a.name; })); } catch (e) { /* не критично */ }
                }
                for (var f = 0; f < 20; f++) {                 // промотка до видимого кадра
                  if (st.state) { st.state.update(0.016); st.state.apply(sk); }
                  sk.update(0.016);
                  sk.updateWorldTransform(0);
                }
                st.last = 0;
                st.raf = requestAnimationFrame(loop);
                return true;
              } catch (e) {
                lastErr = e;
                return false;
              }
            });
          });
        }, Promise.resolve(false)).then(function (ok) {
          if (!ok && st.host === host) {
            if (window.console && console.warn) {
              console.warn("spine-player:", key, (lastErr && lastErr.message) || "не удалось");
            }
            stop();
          }
        });
      });
    }).catch(function (e) {
      if (window.console && console.warn) console.warn("spine-player:", (e && e.message) || e);
      if (st.host === host) stop();
    });
  }

  // Переключение анимации на лету: значение — индекс или имя.
  function select(value) {
    if (!st.skeleton || !st.anims || !st.anims.length) return false;
    var idx = typeof value === "number" ? value : -1;
    if (idx < 0) {
      for (var i = 0; i < st.anims.length; i++) {
        if (st.anims[i].name === value || String(i) === String(value)) { idx = i; break; }
      }
    }
    if (idx < 0 || idx >= st.anims.length) return false;
    st.current = idx;
    if (st.state) st.state.setAnimation(0, st.anims[idx].name, true);
    else if (st.skeleton.setAnimation) st.track = st.skeleton.setAnimation(0, st.anims[idx].name, true);
    return true;
  }

  function current() {
    return st.current;
  }

  function stop() {
    st.host = null;
    st.last = 0;
    drop();
  }

  function readVersion(jsonText) {
    try {
      var d = JSON.parse(jsonText);
      if (d && d.skeleton && d.skeleton.spine) return d.skeleton.spine;
    } catch (e) { /* версию не угадали — возьмём 3.8 по умолчанию */ }
    return "3.8";
  }

  function debug() {
    return {
      трек: st.state && st.state.getCurrent(0)
        ? st.state.getCurrent(0).animation.name
        : (st.anims && st.anims[0] ? st.anims[0].name : null),
      время: st.state && st.state.getCurrent(0)
        ? Math.round(st.state.getCurrent(0).trackTime * 100) / 100
        : null,
      dt: Math.round(st.dt * 1000) / 1000, идёт: !!st.raf,
      страниц: st.pages,
      регионов: st.regions,
      стекстур: st.textured,
      слотов: st.slots,
      прикреплено: st.attached,
      картинок: st.imgs,
      границы: st.bounds
    };
  }

  window.SpineCardPlayer = { play: play, stop: stop, select: select, current: current, version: readVersion, pick: pickRuntime, debug: debug };
})();
