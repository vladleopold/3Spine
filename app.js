/* Public, frontend-only interface. Private 2Spine animation and Linux Spine code is NOT included. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const STUDIO = 'https://spine-studio.spine-animator-f8692224.workers.dev';
  const EXPORTER = 'https://spine-generator.spine-animator-f8692224.workers.dev';
  let downloadURL = null;
  const LIMIT = 180 * 1024 * 1024;
  function message(text) { $('status').textContent = text; }
  function clearDownload() {
    if (downloadURL) URL.revokeObjectURL(downloadURL);
    downloadURL = null;
    $('download').style.display = 'none';
  }
  async function fetchLimited(url, init, limit = LIMIT) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, { ...init, credentials: 'omit', mode: 'cors',
        redirect: 'error', signal: controller.signal });
      if (!response.ok) throw Error('Remote HTTP ' + response.status);
      const expected = Number(response.headers.get('content-length'));
      if (expected > limit) throw Error('Response is too large');
      const bytes = await response.arrayBuffer();
      if (bytes.byteLength > limit) throw Error('Response is too large');
      return { response, bytes };
    } finally { clearTimeout(timeout); }
  }
  function sourceURL(text) {
    const u = new URL(text);
    if (u.protocol !== 'https:' || u.username || u.password || !/\.psd$/i.test(u.pathname))
      throw Error('Provide a direct HTTPS .psd file URL');
    if (['localhost', '127.0.0.1', '[::1]'].includes(u.hostname))
      throw Error('Private network URLs are not supported');
    return u;
  }
  async function getBytes() {
    const file = $('file').files[0];
    if (file) {
      if (file.size > LIMIT) throw Error('PSD is larger than 180 MB');
      return file.arrayBuffer();
    }
    const u = sourceURL($('url').value.trim());
    const endpoint = ['files.catbox.moe', 'litter.catbox.moe'].includes(u.hostname)
      ? EXPORTER + '/api/psd-source?url=' + encodeURIComponent(u.href) : u.href;
    return (await fetchLimited(endpoint, { method: 'GET' })).bytes;
  }
  function canvasOf(img) {
    if (!img) return null;
    if (img instanceof HTMLCanvasElement) return img;
    const width = Number(img.width), height = Number(img.height);
    if (!(width > 0 && height > 0)) return null;
    const c = document.createElement('canvas');
    c.width = width; c.height = height;
    if (img.data && img.data.length === width * height * 4)
      c.getContext('2d').putImageData(new ImageData(new Uint8ClampedArray(img.data), width, height), 0, 0);
    else if (img.canvas instanceof HTMLCanvasElement) c.getContext('2d').drawImage(img.canvas, 0, 0);
    else return null;
    return c;
  }
  function parse(buffer) {
    const signature = new Uint8Array(buffer, 0, Math.min(buffer.byteLength, 4));
    if (signature.length !== 4 || String.fromCharCode(...signature) !== '8BPS')
      throw Error('Invalid PSD header');
    const parser = window.agPsd || window['ag-psd'];
    if (!parser?.readPsd) throw Error('PSD parser failed to load');
    const psd = parser.readPsd(buffer, { useImageData: true, skipThumbnail: true });
    if (!(psd.width > 0 && psd.height > 0) || psd.width * psd.height > 25000000)
      throw Error('Unsupported PSD dimensions');
    const composite = canvasOf(psd.imageData || psd.canvas || psd);
    if (!composite) throw Error('PSD composite is unavailable');
    const layers = [], names = new Set();
    function walk(container, visible = true, opacity = 1) {
      for (const layer of (container.children || []).slice().reverse()) {
        const enabled = visible && layer.hidden !== true;
        if (!enabled) continue;
        if (layer.blendMode && !['normal', 'pass through'].includes(layer.blendMode))
          throw Error('Rasterize unsupported blend mode: ' + layer.blendMode);
        let alpha = layer.opacity === undefined ? 1 : Number(layer.opacity);
        if (alpha > 1) alpha /= 255;
        alpha = Math.max(0, Math.min(1, alpha * opacity));
        if (layer.children && layer.children.length) { walk(layer, enabled, alpha); continue; }
        const original = canvasOf(layer.imageData || layer.canvas || layer);
        if (!original) continue;
        const canvas = document.createElement('canvas');
        canvas.width = original.width; canvas.height = original.height;
        const context = canvas.getContext('2d');
        context.globalAlpha = alpha; context.drawImage(original, 0, 0);
        let id = String(layer.name || 'layer').replace(/[^\w.-]/g, '_').slice(0,70) || 'layer';
        const base = id; let suffix = 1;
        while (names.has(id)) id = base + '_' + suffix++;
        names.add(id);
        layers.push({ id, image: canvas.toDataURL('image/png'),
          bounds: { x: Number(layer.left || 0), y: Number(layer.top || 0),
            width: canvas.width, height: canvas.height } });
      }
    }
    walk(psd);
    if (!layers.length || layers.length > 250) throw Error('PSD needs 1..250 visible raster layers');
    return { width: psd.width, height: psd.height, composite, layers };
  }
  async function create() {
    clearDownload();
    $('create').disabled = true;
    try {
      const motion = $('prompt').value.trim();
      if (!motion || motion.length > 20000) throw Error('Enter a motion description (up to 20,000 characters)');
      message('Loading PSD…');
      const source = parse(await getBytes());
      $('info').textContent = source.width + ' × ' + source.height + ' · ' + source.layers.length + ' layers';
      const preview = $('preview');
      preview.width = source.width; preview.height = source.height;
      preview.getContext('2d').drawImage(source.composite, 0, 0);
      preview.style.display = 'block';
      const request = JSON.stringify({ image: source.composite.toDataURL('image/png'),
        layers: source.layers, description: motion,
        instructions: 'Preserve original draw order and all source layer pixels. Keep frame-zero setup pose. Return complete animated Spine scene. Do not fake 360-degree depth.' });
      if (request.length > 19 * 1024 * 1024) throw Error('PSD exceeds cloud planning limit');
      message('Cloud animation in progress…');
      const planResponse = await fetchLimited(STUDIO + '/api/plan', {
        method: 'POST', headers: { 'content-type': 'application/json' }, body: request
      }, 20 * 1024 * 1024);
      const plan = JSON.parse(new TextDecoder().decode(planResponse.bytes));
      if (!plan.scene || !Array.isArray(plan.scene.clips) || !plan.scene.clips.length)
        throw Error('Cloud did not return complete animation clips. No incomplete ZIP created.');
      if (plan.validation?.initialPosePreserved !== true || plan.validation?.drawOrderPreserved !== true)
        throw Error('Cloud failed source-preservation validation');
      const exportBody = JSON.stringify({ canvas: { width: source.width, height: source.height },
        scene: plan.scene, layers: source.layers });
      if (exportBody.length > 19 * 1024 * 1024) throw Error('Export payload is too large');
      message('Exporting Spine ZIP…');
      const result = await fetchLimited(EXPORTER + '/api/build', {
        method: 'POST', headers: { 'content-type': 'application/json' }, body: exportBody
      }, 200 * 1024 * 1024);
      const sig = new Uint8Array(result.bytes, 0, 4);
      if (sig.length < 4 || sig[0] !== 80 || sig[1] !== 75 || sig[2] !== 3 || sig[3] !== 4)
        throw Error('Server did not return a valid ZIP header');
      downloadURL = URL.createObjectURL(new Blob([result.bytes], { type: 'application/zip' }));
      $('download').href = downloadURL;
      $('download').style.display = 'inline-block';
      message('ZIP ready. Check animations in Spine Editor 4.3 before production.');
    } catch (error) {
      message('Generation failed: ' + (error?.name === 'AbortError' ? '15-second network timeout' : error.message));
    } finally {
      $('create').disabled = false;
    }
  }
  $('create').addEventListener('click', create);
  $('file').addEventListener('change', () => { $('url').value = ''; clearDownload(); });
  $('url').addEventListener('change', () => { $('file').value = ''; clearDownload(); });
  $('prompt').addEventListener('input', clearDownload);
})();
