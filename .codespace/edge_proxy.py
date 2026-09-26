#!/usr/bin/env python3
"""edge_proxy.py — свой локальный прокси, который туннелирует через edge Cloudflare.

Зачем он нам: площадки блокируют IP дата-центров, а edge-адреса Cloudflare
не блокируют. Вместо чужих публичных прокси (они умирают за часы) поднимаем
свой MITM-прокси на 127.0.0.1 и проксируем запросы через воркер:

    браузер → --proxy-server=127.0.0.1:PORT → воркер /proxy?url=… → сайт

Сертификат для MITM — самоподписанный, Chromium запускается с
--ignore-certificate-errors, поэтому содержимое для браузера валидно.
"""
import asyncio
import os
import re
import ssl
import subprocess
import sys
import threading
import urllib.parse

BROKER = os.environ.get(
    "SPINE_EDGE_PROXY",
    "https://spine-broker.leopolds2010.workers.dev/proxy?url=").strip()
CERT_DIR = os.environ.get("SPINE_MITM_DIR", "/tmp/mitm")


def ensure_cert(host: str) -> tuple:
    """Готовим сертификат для хоста (самоподписанный, кэшируем на диск)."""
    os.makedirs(CERT_DIR, exist_ok=True)
    key = os.path.join(CERT_DIR, host + ".key")
    crt = os.path.join(CERT_DIR, host + ".crt")
    if os.path.exists(key) and os.path.exists(crt):
        return key, crt
    cnf = os.path.join(CERT_DIR, host + ".cnf")
    with open(cnf, "w") as f:
        f.write("[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n"
                "[dn]\nCN=%s\n[v3]\nsubjectAltName=DNS:%s,DNS:*.%s\n"
                "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n"
                "extendedKeyUsage=serverAuth\n" % (host, host, host))
    try:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", key, "-out", crt, "-days", "3650", "-config", cnf],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:                                     # noqa: BLE001
        return "", ""
    return key, crt


async def upstream(url: str, method: str, headers: dict, body: bytes) -> tuple:
    """Забираем ответ через edge-прокси воркера."""
    target = BROKER + urllib.parse.quote(url, safe="")
    hdrs = {"User-Agent": headers.get("user-agent", "Mozilla/5.0"),
            "Accept": headers.get("accept", "*/*")}
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", UPSTREAM, target, method,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(body or b""), timeout=45)
    except Exception:                                     # noqa: BLE001
        return 502, {}, b"upstream failed"
    head, _, payload = out.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    try:
        status = int(lines[0].split()[1])
    except Exception:                                     # noqa: BLE001
        return 502, {}, b"bad upstream"
    hdr = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        hdr[k.strip().lower()] = v.strip()
    return status, hdr, payload


UPSTREAM = r'''
import sys, urllib.request
url, method = sys.argv[1], sys.argv[2]
body = sys.stdin.buffer.read()
req = urllib.request.Request(url, data=body or None, method=method, headers={
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"})
try:
    with urllib.request.urlopen(req, timeout=45) as r:
        data = r.read()
        hdrs = ";".join("%s: %s" % (k.lower(), v) for k, v in r.headers.items())
        sys.stdout.buffer.write(("HTTP/1.1 %d OK\r\nX-Real-Headers: %s\r\n\r\n"
                                 % (r.status, hdrs)).encode("latin-1") + data)
except urllib.error.HTTPError as e:
    data = e.read()
    sys.stdout.buffer.write(("HTTP/1.1 %d Error\r\nX-Real-Headers: \r\n\r\n"
                             % e.code).encode("latin-1") + data)
except Exception as e:
    sys.stdout.buffer.write(("HTTP/1.1 502 Bad Gateway\r\nX-Real-Headers: \r\n\r\n"
                             + str(e).encode()).encode())
'''


def free_port(preferred: int = 8899) -> int:
    """Занятый порт не беда — берём следующий свободный."""
    import socket
    for p in [preferred] + [preferred + i for i in range(1, 40)]:
        with socket.socket() as sk:
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sk.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return preferred


def serve(port: int = 8899) -> int:
    """Запускает локальный прокси в потоке; возвращает реальный порт."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    nonlocal_resolved = {"port": port}
    port = free_port(port)

    async def main():
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=20)
                if not line:
                    writer.close()
                    return
                parts = line.decode("latin-1").split()
                if len(parts) < 3:
                    writer.close()
                    return
                method, target = parts[0], parts[1]
                headers = {}
                while True:
                    h = await asyncio.wait_for(reader.readline(), timeout=20)
                    if h in (b"\r\n", b"\n", b""):
                        break
                    k, _, v = h.decode("latin-1").partition(":")
                    headers[k.strip().lower()] = v.strip()
                if method.upper() == "CONNECT":
                    host = target.split(":")[0]
                    key, crt = ensure_cert(host)
                    if not key:
                        writer.close()
                        return
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    ctx.load_cert_chain(crt, key)
                    try:
                        await writer.start_tls(ctx)
                    except Exception:                     # noqa: BLE001
                        writer.close()
                        return
                    inner = await asyncio.wait_for(read_request(reader), timeout=30)
                    if not inner:
                        writer.close()
                        return
                    m2, path2, h2, b2 = inner
                    full = "https://%s%s" % (host, path2)
                    st, hr, payload = await upstream(full, m2, h2, b2)
                    ctype = hr.get("x-real-headers", "")
                    m3 = re.search(r"content-type:\s*([^;]+)", ctype, re.I)
                    ct = m3.group(1).strip() if m3 else "application/octet-stream"
                    writer.write(("HTTP/1.1 %d X\r\nContent-Type: %s\r\n"
                                  "Content-Length: %d\r\nAccess-Control-Allow-Origin: *\r\n"
                                  "Connection: close\r\n\r\n" % (st, ct, len(payload)))
                                 .encode("latin-1") + payload)
                    await writer.drain()
                else:
                    url = target if target.startswith("http") else "http://" + target
                    st, hr, payload = await upstream(url, method, headers, b"")
                    writer.write(("HTTP/1.1 %d X\r\nContent-Length: %d\r\n"
                                  "Connection: close\r\n\r\n" % (st, len(payload)))
                                 .encode("latin-1") + payload)
                    await writer.drain()
            except Exception:                             # noqa: BLE001
                pass
            finally:
                try:
                    writer.close()
                except Exception:                         # noqa: BLE001
                    pass

        async def read_request(r: asyncio.StreamReader):
            line = await asyncio.wait_for(r.readline(), timeout=20)
            if not line:
                return None
            parts = line.decode("latin-1").split()
            if len(parts) < 3:
                return None
            hdrs = {}
            while True:
                h = await asyncio.wait_for(r.readline(), timeout=20)
                if h in (b"\r\n", b"\n", b""):
                    break
                k, _, v = h.decode("latin-1").partition(":")
                hdrs[k.strip().lower()] = v.strip()
            return parts[0], parts[1], hdrs, b""

        srv = await asyncio.start_server(handle, "127.0.0.1", port)
        nonlocal_resolved["port"] = port
        ready.set()
        async with srv:
            await srv.serve_forever()

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())

    t = threading.Thread(target=run, daemon=True)
    t.start()
    ready.wait(timeout=20)
    return nonlocal_resolved["port"]


if __name__ == "__main__":
    p = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8899)
    print("edge-proxy слушает 127.0.0.1:%d" % p, flush=True)
    while True:
        import time
        time.sleep(3600)
