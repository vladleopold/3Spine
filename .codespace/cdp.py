"""Минимальный клиент Chrome DevTools Protocol без внешних зависимостей.

Нужен, чтобы узнать, какие ассеты игра грузит по-настоящему: сеть, клики,
консоль. Реализация WebSocket (RFC 6455) здесь предельно простая — клиент
шлёт только текстовые кадры и читает всё, что приходит.
"""
import base64
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import time
import urllib.request

NORMAL_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
CHROME = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "/usr/bin/google-chrome", "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
# кнопки «играть», которые надо нажать, чтобы игра реально стартовала
HOOK_JS = r"""
(function(){
  if (window.__g) return;
  var seen = window.__g = [];
  var push = function(v){
    try{
      if(!v) return;
      if (typeof v !== 'string') v = (v && (v.url || v.href)) || String(v);
      if (v.indexOf('http') === 0 || /\.(skel|json|atlas|png|webp|ktx2?|basis|data|bin|bundle)(\?|$)/i.test(v))
        seen.push(v);
    }catch(_){}
  };
  try {
    performance.getEntriesByType('resource').forEach(function(e){ push(e.name); });
    new PerformanceObserver(function(list){
      list.getEntries().forEach(function(e){ push(e.name); });
    }).observe({type:'resource', buffered:true});
  } catch(_){}
  var of = window.fetch;
  window.fetch = function(i){
    try{ push(typeof i === 'string' ? i : (i && i.url)); }catch(_){}
    return of.apply(this, arguments);
  };
  var XO = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(m,u){
    try{ push(u); }catch(_){}
    return XO.apply(this, arguments);
  };
  if (window.URL && URL.createObjectURL) {
    var co = URL.createObjectURL;
    URL.createObjectURL = function(b){ var u = co.apply(this, arguments); push(u); return u; };
  }
  try {
    var sd = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype,'src');
    if (sd && sd.set) {
      Object.defineProperty(HTMLImageElement.prototype,'src',{
        get: sd.get,
        set: function(v){ push(v); return sd.set.call(this,v); }
      });
    }
  } catch(_){}
})();
"""
CLICK_SQL = (
    "document.querySelectorAll('button,a,div,span').forEach(e=>{const t=(e.innerText||"
    "e.textContent||'').trim().toLowerCase();if(e.offsetParent&&t.length<24&&(/^(play|start|"
    "play now|play free|запустить|играть|начать|go|enter|play game|demo|watch)/.test(t))){"
    "e.click();}});"
)
DISMISS_SQL = (
    "document.querySelectorAll('[id*=accept],[class*=accept],[id*=consent],"
    "[class*=consent],[id*=cookie],[class*=cookie],[aria-label*=Accept i]').forEach("
    "e=>{try{e.click()}catch(_){_}});"
)


def find_chrome() -> str:
    for c in CHROME:
        p = shutil.which(c) if not c.startswith("/") else (c if os.path.exists(c) else None)
        if p:
            return p
    return ""


class WS:
    """Клиентский WebSocket: connect, send_text, recv (достаточно для CDP)."""

    def __init__(self, url: str, timeout: float = 20.0):
        m = re.match(r"ws://([^:/]+):(\d+)(/.*)", url)
        host, port, path = m.group(1), int(m.group(2)), m.group(3)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key))
        self.sock.sendall(req.encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("ws handshake failed")
            self.buf += chunk
        self.buf = self.buf.split(b"\r\n\r\n", 1)[1]

    def _recv_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise OSError("ws closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, data: str) -> None:
        payload = data.encode()
        n = len(payload)
        header = b"\x81"
        if n < 126:
            header += struct.pack("!B", 0x80 | n)
        elif n < 65536:
            header += struct.pack("!BH", 0x80 | 126, n)
        else:
            header += struct.pack("!BQ", 0x80 | 127, n)
        mask = secrets.token_bytes(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def recv(self) -> str:
        while True:
            b1, b2 = self._recv_exact(2)
            opcode, length = b1 & 0x0F, b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            data = b"" if opcode & 0x08 else self._recv_exact(length)
            if opcode & 0x08:
                raise OSError("ws closed by peer")
            if opcode in (1, 2):
                return data.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def port_alive(port: int) -> bool:
    sk = socket.socket()
    sk.settimeout(1.5)
    try:
        sk.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sk.close()


def free_port(preferred: int = 9333) -> int:
    """Если порт занут (старый Chrome) — берём свободный, чтобы не ловить 0 кандидатов."""
    if not port_alive(preferred):
        return preferred
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


class Crawler:
    """Гоняет игру в браузере и собирает все сетевые запросы."""

    def __init__(self, budget_ms: int = 25000, port: int = 9333, click: bool = False):
        self.budget_ms = max(int(budget_ms), 18000)
        self.click = click
        self.port = port
        self.urls = set()
        self.notes = []
        self.sessions = set()
        self.targets = {}

    def _http_json(self, path: str):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path), timeout=5) as r:
            return json.loads(r.read().decode())

    def run(self, url: str, lognet: str = "", attempt: int = 0) -> dict:
        chrome = find_chrome()
        if not chrome:
            return {"urls": [], "notes": ["chrome не найден"]}
        self.port = free_port(self.port)
        if attempt:                      # повтор: даём больше времени
            self.budget_ms = int(self.budget_ms * 1.6)
        if not lognet:
            lognet = "/tmp/cdp-net-%d.json" % os.getpid()
        self.netlog = lognet
        prof = "/tmp/cdp-profile-%d" % os.getpid()
        proc = subprocess.Popen(
            [chrome, "--headless=new", "--remote-debugging-port=%d" % self.port,
             "--user-data-dir=" + prof, "--no-first-run", "--no-default-browser-check",
             "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
             "--user-agent=" + NORMAL_UA, "--lang=en-US", "--window-size=1280,900",
             "--enable-unsafe-swiftshader", "--use-gl=angle", "--use-angle=swiftshader",
             "--autoplay-policy=no-user-gesture-required",
             "--disable-features=IsolateOrigins,site-per-process",
             "--disable-extensions", "--mute-audio", "--hide-scrollbars",
             "--virtual-time-budget=%d" % self.budget_ms,
             "--log-net-log=" + lognet, url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # ждём, пока DevTools-порт действительно поднимется
        t0 = time.time()
        while time.time() - t0 < 15 and not port_alive(self.port):
            time.sleep(0.3)
        if not port_alive(self.port):
            return {"urls": [], "notes": ["Chrome не слушает порт %d" % self.port]}
        try:
            res = self._drive()
            urls = set(res.get("urls", [])) | set(self._read_netlog())
            self.urls |= urls
            res["urls"] = sorted(urls)
            if not urls and attempt == 0:
                res2 = self.run(url, lognet, attempt=1)
                return res2
            return res
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:                              # noqa: BLE001
                proc.kill()
            shutil.rmtree(prof, ignore_errors=True)

    def _read_netlog(self) -> list:
        """Все запросы с момента старта браузера, включая popup и iframe."""
        path = getattr(self, "netlog", "")
        if not path or not os.path.exists(path):
            return []
        out = set()
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except OSError:
            return []
        for m in re.finditer(r'"url":\s*"([^"]+)"', raw):
            u = m.group(1).replace("\\\\/", "/")
            if u.startswith(("http://", "https://")):
                out.add(u.split("#")[0])
        return sorted(out)

    def _drive(self) -> dict:
        deadline = time.time() + 20
        tabs = None
        while time.time() < deadline:
            try:
                tabs = self._http_json("/json/list")
                break
            except Exception:                              # noqa: BLE001
                time.sleep(0.3)
        if not tabs:
            return {"urls": [], "notes": ["devtools не поднялся"]}
        target = next((t for t in tabs if t.get("type") == "page"), None)
        if not target:
            return {"urls": [], "notes": ["нет вкладки"]}
        ws = WS(target["webSocketDebuggerUrl"], timeout=30)
        mid = [0]

        def cmd(method: str, params=None, session=None):
            mid[0] += 1
            msg = {"id": mid[0], "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            ws.send(json.dumps(msg))
            return mid[0]

        for m in ("Network.enable", "Page.enable", "Runtime.enable"):
            cmd(m)
        cmd("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False,
                                     "flatten": True})
        cmd("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK_JS})
        cmd("Runtime.evaluate", {"expression":
            "JSON.stringify(performance.getEntriesByType('resource').map(function(e){return e.name;}))",
            "returnByValue": True})
        end = time.time() + self.budget_ms / 1000.0
        last_click = 0.0
        last_poll = [0.0]
        while time.time() < end:
            try:
                msg = json.loads(ws.recv())
            except Exception:                              # noqa: BLE001
                break
            sid = msg.get("sessionId")
            if sid and sid not in self.sessions:
                self.sessions.add(sid)
                self.targets[sid] = (msg.get("params", {}).get("targetInfo", {}) or {})
                for m in ("Network.enable", "Runtime.enable", "Page.enable"):
                    cmd(m, {}, sid)
                try:
                    cmd("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK_JS}, sid)
                    cmd("Runtime.evaluate", {"expression":
                        "JSON.stringify(performance.getEntriesByType('resource')"
                        ".map(function(e){return e.name;}))", "returnByValue": True}, sid)
                except Exception:                          # noqa: BLE001
                    pass
            if "id" in msg and "result" in msg:
                try:
                    val = ((msg.get("result", {}).get("result") or {}).get("value"))
                    if isinstance(val, str) and val.startswith("["):
                        for u in json.loads(val):
                            if isinstance(u, str) and u.startswith("http"):
                                self.urls.add(u)
                except Exception:                          # noqa: BLE001
                    pass
            method = msg.get("method")
            if method == "Network.requestWillBeSent":
                u = (msg.get("params", {}).get("request", {}) or {}).get("url", "")
                if u.startswith(("http://", "https://")):
                    self.urls.add(u.split("#")[0])
            elif method == "Runtime.consoleAPICalled":
                txt = " ".join(str(a.get("value", "")) for a in
                                msg.get("params", {}).get("args", []))
                if any(k in txt.lower() for k in ("spine", "atlas", "skel", "error", "fail")):
                    self.notes.append(txt[:200])
            if self.click and time.time() - last_click > 4:
                last_click = time.time()
                for sql in ((DISMISS_SQL, CLICK_SQL) if self.click else ()) + ("JSON.stringify(window.__g||[])",):
                    try:
                        cmd("Runtime.evaluate",
                            {"expression": sql, "awaitPromise": False,
                             "returnByValue": True})
                    except Exception:                      # noqa: BLE001
                        pass
            if time.time() - last_poll[0] > 1.2:
                last_poll[0] = time.time()
                for target in [None] + sorted(self.sessions):
                    try:
                        cmd("Runtime.evaluate",
                            {"expression": "JSON.stringify((window.__g||[]).slice(0,3000))",
                             "returnByValue": True}, target)
                    except Exception:                      # noqa: BLE001
                        pass
        ws.close()
        return {"urls": sorted(self.urls), "notes": self.notes[:20],
                "targets": [t.get("url", "") for t in self.targets.values() if t.get("url")]}
