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
    st.liftedTried = false;
    st.lifted = false;
    st.patched = 0;
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
  function buildCanvas(ns, assets, images) {
    if (!ns.SkeletonRenderer || !ns.CanvasTexture) throw new Error("в сборке нет канвас-рендерера");
    var atlas = new ns.TextureAtlas(assets.atlas);
    var pages = atlas.pages || [];
    if (pages.length > images.length) throw new Error("в атласе " + pages.length + " листов, а есть " + images.length);
    pages.forEach(function (page, i) { page.setTexture(new ns.CanvasTexture(images[i])); });
    var json = JSON.parse(assets.json);
    st.patched = patchMissingRegions(ns, atlas, json);
    var loader = new ns.AtlasAttachmentLoader(atlas);
    var sd = new ns.SkeletonJson(loader).readSkeletonData(json);
    var sk = new ns.Skeleton(sd);
    sk.setToSetupPose();
    sk.setSkin(0);
    sk.setSlotsToSetupPose();
    st.skeleton = sk;
    st.anims = sd.animations || [];
    // В сборках 4.x список регионов один на весь атлас, у листов его нет
    st.pages = pages.length;
    st.regions = (atlas.regions || []).length;
    st.textured = (atlas.regions || []).filter(function (r) { return !!r.texture; }).length;
    st.slots = (sd.slots || []).length;
    st.attached = sk.slots.filter(function (s) { return !!s.attachment; }).length;
    st.imgs = images.map(function (i) { return i.naturalWidth + "x" + i.naturalHeight; }).join(",");
    st.names = (atlas.regions || []).slice(0, 3).map(function (r) { return r.name; }).join(",");
    st.db = (function () {
      var d = sk.data.bounds;
      return d ? Math.round(d.width) + "x" + Math.round(d.height) + "@" + Math.round(d.x) + "," + Math.round(d.y) : "null";
    })();
    st.bounds = (function () {
      var x = sk.data.x, y = sk.data.y, w = sk.data.width, h = sk.data.height;
      return w + "x" + h + "@" + x + "," + y;
    })();
    st.root = (function () {
      var b = sk.data.bones && sk.data.bones[0];
      return b ? Math.round(b.x) + "," + Math.round(b.y) : "?";
    })();
    st.state = ns.AnimationState ? new ns.AnimationState(new ns.AnimationStateData(sd)) : null;
    st.renderer = new ns.SkeletonRenderer(st.g2);
    if (hasNonRegion(st.renderer, sd)) st.renderer.triangleRendering = true;
    return sk;
  }

  // Штатный путь отрисовки в сборках 4.x (drawImages) рисует ТОЛЬКО вложения
  // вида RegionAttachment — всё остальное (mesh, linkedmesh, path, clipping)
  // молча пропускается:  if (!(attachment instanceof RegionAttachment)) continue;
  // Поэтому скелет, у которого вся графика — меши, рисуется пустым холстом без
  // единой ошибки (проверено на PragmaticPlay vs20wraanu, wran_hv_1: 111 mesh,
  // 0 region — 0 пикселей). Треугольный путь умеет и те, и другие, но он
  // дороже, поэтому включаем его только когда меши в скинах действительно есть.
  function hasNonRegion(renderer, sd) {
    if (!("triangleRendering" in renderer) || !sd || !sd.skins) return false;
    for (var i = 0; i < sd.skins.length; i++) {
      var att = sd.skins[i].attachments;
      if (!att) continue;
      for (var slot in att) {
        var byName = att[slot];
        if (!byName) continue;
        for (var name in byName) {
          var a = byName[name];
          if (!a || !a.constructor) continue;
          // у минифицированных сборок класс зовётся _RegionAttachment
          if (!/RegionAttachment$/.test(a.constructor.name)) return true;
        }
      }
    }
    return false;
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

  // Границы по факту отрисованного: у части скелетов data.bounds пустой или
  // вырожденный (0x0, 2x2), а координаты костей уходят в минус — без нормального
  // фита картинка рисуется за пределами канваса и карточка остаётся пустой.
  function contentBounds(skeleton) {
    var minx = 1e9, miny = 1e9, maxx = -1e9, maxy = -1e9, any = false;
    function take(x, y, w, h) {
      any = true;
      if (x - w < minx) minx = x - w;
      if (y - h < miny) miny = y - h;
      if (x + w > maxx) maxx = x + w;
      if (y + h > maxy) maxy = y + h;
    }
    function point(x, y) {
      any = true;
      if (x < minx) minx = x;
      if (x > maxx) maxx = x;
      if (y < miny) miny = y;
      if (y > maxy) maxy = y;
    }
    skeleton.bones.forEach(function (bone) {
      take(bone.worldX, bone.worldY, 0, 0);
    });
    // Локальная точка вложения → координаты скелета через трансформ кости.
    // Рантайм считает мировые вершины меша только во время отрисовки и в
    // само вложение их не кладёт, поэтому для границ считаем сами: иначе у
    // скелетов, собранных из одних мешей, рамка выходит по точкам костей,
    // графика рисуется за её пределами, и карточка выглядит пустой.
    var pt = [0, 0];
    function toWorld(sl, x, y) {
      var b = sl.bone;
      if (!b) { pt[0] = x; pt[1] = y; return; }
      pt[0] = x * b.a + y * b.c + b.worldX;
      pt[1] = x * b.b + y * b.d + b.worldY;
    }
    skeleton.slots.forEach(function (sl) {
      var a = sl.attachment; if (!a) return;
      var wv = a.worldVertices;
      if (wv && wv.length > 3) {
        for (var i = 0; i + 1 < wv.length; i += 2) point(wv[i], wv[i + 1]);
        return;
      }
      var reg = a.region;
      var rw = (reg && reg.region && reg.region.width) || a.width || 0;
      var rh = (reg && reg.region && reg.region.height) || a.height || 0;
      if (rw > 0 && rh > 0) {
        // Прямоугольник картинки с её собственным поворотом и сдвигом.
        var cs = Math.cos(a.rotation || 0), sn = Math.sin(a.rotation || 0);
        var sx = a.scaleX == null ? 1 : a.scaleX, sy = a.scaleY == null ? 1 : a.scaleY;
        var ax = a.x || 0, ay = a.y || 0;
        var cs4 = [[0, 0], [rw, 0], [rw, rh], [0, rh]];
        for (var k = 0; k < 4; k++) {
          var lx = cs4[k][0] * sx, ly = cs4[k][1] * sy;
          toWorld(sl, lx * cs - ly * sn + ax, lx * sn + ly * cs + ay);
          point(pt[0], pt[1]);
        }
        return;
      }
      var vs = a.vertices;
      if (vs && vs.length > 1) {
        for (var j = 0; j + 1 < vs.length; j += 2) { toWorld(sl, vs[j], vs[j + 1]); point(pt[0], pt[1]); }
        return;
      }
      if (sl.bone) take(sl.bone.worldX, sl.bone.worldY, rw / 2, rh / 2);
    });
    if (!any) return null;
    if (maxx - minx < 4) { minx -= 12; maxx += 12; }
    if (maxy - miny < 4) { miny -= 12; maxy += 12; }
    return { x: minx, y: miny, width: maxx - minx, height: maxy - miny };
  }

  // Границы из данных скелетона, но всегда с числами: в JSON скелета x/y часто
  // нет вовсе, и если такое undefined уедет в fit(), сдвиг станет NaN, а холст с
  // NaN-трансформом не рисует ничего — карточка молча остаётся пустой.
  function num(v, dflt) { return typeof v === "number" && isFinite(v) ? v : dflt; }

  function pickBounds(skeleton) {
    var d = skeleton.data;
    var c = contentBounds(skeleton);
    if (d.bounds && d.bounds.width > 1 && d.bounds.height > 1) {
      return { x: num(d.bounds.x, 0), y: num(d.bounds.y, 0),
               width: d.bounds.width, height: d.bounds.height, src: "data.bounds" };
    }
    if (d.width > 1 && d.height > 1) {
      return { x: num(d.x, 0), y: num(d.y, 0),
               width: d.width, height: d.height, src: "data" };
    }
    if (c) { c.src = "по содержимому"; return c; }
    return null;
  }

  function fit(skeleton) {
    if (skeleton.updateWorldTransform) skeleton.updateWorldTransform(0);
    var b = pickBounds(skeleton);
    if (!b || !(b.width > 0) || !(b.height > 0)) return;
    // Границы из данных скелетонаописывают только «дизайн» размера, а реальная
    // графика (особенно меши) часто вылезает за них — тогда карточка рисует
    // вьюпорт, где ничего нет, и выглядит пустой. Поэтому берём объединение:
    // расширяем рамку, но никогда не сжимаем её.
    var c = contentBounds(skeleton);
    if (c && c.width > 0 && c.height > 0) {
      var x0 = Math.min(b.x, c.x), y0 = Math.min(b.y, c.y);
      var x1 = Math.max(b.x + b.width, c.x + c.width);
      var y1 = Math.max(b.y + b.height, c.y + c.height);
      if (x1 - x0 > b.width + 1 || y1 - y0 > b.height + 1) {
        b = { x: x0, y: y0, width: x1 - x0, height: y1 - y0,
              src: b.src + "+содержимое" };
      }
    }
    var pad = 10;
    var w = st.cssW || st.canvas.width, h = st.cssH || st.canvas.height;
    var scale = Math.min((w - pad * 2) / b.width, (h - pad * 2) / b.height);
    if (!isFinite(scale) || scale <= 0) return;
    skeleton.scaleX = scale;
    skeleton.scaleY = scale;
    skeleton.x = (w - b.width * scale) / 2 - num(b.x, 0) * scale;
    skeleton.y = (h - b.height * scale) / 2 - num(b.y, 0) * scale;
    // Страховка от NaN в сдвиге: с ним холст не рисует вообще ничего.
    if (!isFinite(skeleton.x)) skeleton.x = (w - b.width * scale) / 2;
    if (!isFinite(skeleton.y)) skeleton.y = (h - b.height * scale) / 2;
    st.fitInfo = {
      w: Math.round(b.width), h: Math.round(b.height),
      x: Math.round(num(b.x, 0)), y: Math.round(num(b.y, 0)),
      масштаб: Math.round(scale * 1000) / 1000,
      сдвиг: Math.round(skeleton.x) + "," + Math.round(skeleton.y),
      источник: b.src || "по содержимому"
    };
  }

  // Пустой кадр — значит рантайм не тянет эти данные: лучше статичное
  // превью, чем пустая карточка под курсором. Проверять надо не на первом
  // кадре, а после прогрева: у многих анимаций (00_start, activation)
  // первый кадр пуст по определению — они появляются из прозрачности.
  function frameEmpty() {
    try {
      var d = st.g2.getImageData(0, 0, st.canvas.width, st.canvas.height).data;
      for (var i = 3; i < d.length; i += 4 * 11) {
        if (d[i] > 8) return false;
      }
      return true;
    } catch (e) {
      return false;                       // не смогли прочитать — не мешаем
    }
  }

  var WARMUP = 45;                        // кадров до первого вывода

  // Некоторые панели приходят с вложениями, которых нет в атласе: атлас
  // пересобирался из страниц игры и часть регионов в него не попала, а
  // загрузчик на этом падает целиком. Такие слоты закрываем пустышкой, чтобы
  // остальная панель всё равно играла.
  function patchMissingRegions(ns, atlas, json) {
    if (!ns.TextureRegion || !atlas.pages.length) return 0;
    var want = {};
    var skins = json.skins;
    (Array.isArray(skins) ? skins : []).forEach(function (skin) {
      var att = skin && skin.attachments;
      Object.keys(att || {}).forEach(function (slotName) {
        var slot = att[slotName] || {};
        Object.keys(slot).forEach(function (key) {
          var a = slot[key] || {};
          // У Playson (и вообще в 3.8) у region-вложения нет ни path, ни image,
          // а у части записей нет и type: путь равен ключу. Mesh-вложение ищет
          // регион по тому же имени, поэтому подмена одна на оба типа.
          var path = a.path || a.image || key;
          if (path) want[path] = true;
        });
      });
    });
    var made = 0;
    Object.keys(want).forEach(function (path) {
      if (atlas.findRegion(path)) return;
      var r = new ns.TextureRegion();
      r.name = path;
      r.page = atlas.pages[0];
      r.originalWidth = 1;
      r.originalHeight = 1;
      atlas.regions.push(r);
      made += 1;
    });
    return made;
  }

  // Панели часто держат альфу в setup-позе на нуле, а rgba-таймлайна, который
  // её поднимает, в анимации нет: в игре цвет ставит код. В карточке такой
  // слот остаётся полностью прозрачным — показываем его.
  function liftInvisibleSlots() {
    var n = 0;
    (st.skeleton.slots || []).forEach(function (slot) {
      if (slot.color && slot.color.a === 0 && slot.getAttachment && slot.getAttachment()) {
        slot.color.a = 1;
        n += 1;
      }
    });
    return n;
  }

  function tooEmpty() {
    if (st.saw) return false;             // хоть раз что-то нарисовалось
    st.frames = (st.frames || 0) + 1;
    if (st.frames < WARMUP) return false;
    if (!frameEmpty()) { st.saw = true; return false; }
    if (!st.liftedTried) {
      // поднимаем альфу один раз: если поднимать нечего, повторять бессмысленно
      // и вердикт о пустом кадре никогда не выносится
      st.liftedTried = true;
      st.lifted = liftInvisibleSlots() > 0;
      return false;
    }
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
    // В 4.1+ у скелета нет update(): там поза целиком считается в
    // updateWorldTransform. Вызываем то, что есть, — иначе 4.x-игры падают.
    if (typeof st.skeleton.update === "function") st.skeleton.update(st.dt);
    st.skeleton.updateWorldTransform(0);
    st.g2.clearRect(0, 0, st.cssW || st.canvas.width, st.cssH || st.canvas.height);
    st.renderer.draw(st.skeleton);
    // Первый кадр рисуется ещё без развёрнутых вершин мешей, поэтому рамку
    // пересчитываем один раз уже после отрисовки — иначе объединение с
    // содержимым ничего не добавит.
    if (!st.refit) { st.refit = 1; fit(st.skeleton); }
    if (tooEmpty()) {
      // пусто на всём прогреве: говорим карточке почему, чтобы она не молчала
      st.reason = st.patched
        ? "не хватает " + st.patched + " картинок в атласе — показан кадр"
        : "в игре это пустая заглушка — показан кадр";
      stop();
      return;
    }
    st.raf = requestAnimationFrame(loop);
  }

  // assets: { json, atlas, pages: [Blob, ...] }
  function play(host, assetsPromise, onReady) {
    stop();
    st.reason = "";
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
                  if (typeof sk.update === "function") sk.update(0.016);
                  sk.updateWorldTransform(0);
                }
                // Фитим по позе после промотки: в покое геометрия может лежать
                // в нуле, а в кадре анимации — далеко за пределами канваса.
                fit(sk);
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
              if (lastErr && lastErr.stack && window.console.debug) {
                console.debug("spine-player stack:", lastErr.stack);
              }
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

  // Честный зонд: что реально готово к отрисовке — альфа слотов, кости,
  // текстура и куда ложится геометрия после нашего трансформа.
  function probe() {
    var sk = st.skeleton;
    if (!sk) return { нет: 'скелета' };
    var slots = sk.slots.map(function (sl) {
      var a = sl.attachment;
      var reg = a && a.region;
      var pg = reg && reg.page;
      var img = pg && pg.texture && pg.texture.getImage ? pg.texture.getImage() : null;
      return {
        имя: sl.data.name,
        кость: sl.bone ? Math.round(sl.bone.worldX) + "," + Math.round(sl.bone.worldY) : "-",
        альфа: sl.color ? +(sl.color.a || 0).toFixed(2) : 0,
        вложение: a ? a.name : null,
        вид: a ? (a.type || (a.constructor || {}).name) : null,
        регион: reg ? reg.name : "НЕТ",
        страница: pg ? pg.name : "НЕТ",
        лист: img ? (img.naturalWidth || img.width) + "x" + (img.naturalHeight || img.height) : "НЕТ",
        размерРегиона: reg && reg.region ? Math.round(reg.region.width) + "x" + Math.round(reg.region.height) : "-"
      };
    });
    st.drawOrder = (sk.drawOrder || []).filter(function (sl) {
      return sl.attachment && sl.color && sl.color.a > 0;
    }).length;
    var c = st.g2 && st.g2.canvas;
    var ctx = st.g2;
    var m = (ctx && ctx.getTransform) ? ctx.getTransform() : null;
    return {
      канвас: c ? c.width + "x" + c.height : "-",
      css: c ? c.clientWidth + "x" + c.clientHeight : "-",
      трансформ: m ? [m.a, m.d, m.e, m.f].map(function (v) { return Math.round(v * 100) / 100; }).join(",") : "-",
      слоты: slots, рисуется: st.drawOrder
    };
  }

  // Слоты с вложением, у которого нашёлся регион в атласе: без него карточка
  // пустая, хотя вложение формально есть.
  function sk0slots(sk) {
    var n = 0;
    ((sk && sk.slots) || []).forEach(function (s) {
      if (s.attachment && s.attachment.region) n += 1;
    });
    return n;
  }

  function debug() {
    if (st.skeleton && !st.world) {
      var minx = 1e9, miny = 1e9, maxx = -1e9, maxy = -1e9, n = 0;
      st.skeleton.slots.forEach(function (sl) {
        var at = sl.attachment; if (!at) return;
        var a = Array.isArray(at.worldVertices) ? at.worldVertices : at.worldVerticesLength ? null : null;
        if (sl.worldVertices) {
          for (var i = 0; i < sl.worldVertices.length; i++) {
            var vx = sl.worldVertices[i], vy = sl.worldVertices[i + 1];
            if (vx < minx) minx = vx; if (vx > maxx) maxx = vx;
            if (vy < miny) miny = vy; if (vy > maxy) maxy = vy; n++;
          }
        }
      });
      st.world = n ? [Math.round(minx), Math.round(miny), Math.round(maxx), Math.round(maxy)].join(",") : "нет вершин";
    }
    return {
      мира_слот: st.world,
      трек: st.state && st.state.getCurrent(0)
        ? st.state.getCurrent(0).animation.name
        : (st.anims && st.anims[0] ? st.anims[0].name : null),
      анимаций: (st.anims || []).length,
      анимации: (st.anims || []).slice(0, 3).map(function (a) { return a.name; }).join(","),
      трекЖивой: !!(st.state && st.state.getCurrent(0)),
      сРегионом: (sk0slots(sk) || []),
      время: st.state && st.state.getCurrent(0)
        ? Math.round(st.state.getCurrent(0).trackTime * 100) / 100
        : null,
      dt: Math.round(st.dt * 1000) / 1000, идёт: !!st.raf,
      картинки: (st.images || []).map(function (im) {
        return (im.naturalWidth || im.width || 0) + "x" + (im.naturalHeight || im.height || 0);
      }),
      подменено: st.patched || 0, причина: st.reason || "",
      страниц: st.pages,
      регионов: st.regions, имена: st.names,
      стекстур: st.textured,
      слотов: st.slots,
      прикреплено: st.attached,
      картинок: st.imgs,
      границы: st.bounds, фит: st.fitInfo, dataBounds: st.db, корень: st.root, мира: st.world
    };
  }

  window.SpineCardPlayer = { probe: probe, play: play, stop: stop, select: select, current: current, version: readVersion, pick: pickRuntime, debug: debug, note: function () { return st.reason || ""; } };
})();
