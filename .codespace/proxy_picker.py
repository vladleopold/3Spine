#!/usr/bin/env python3
"""proxy_picker.py — автоподбор рабочего прокси без участия человека.

Берём публичные списки, проверяем кандидатов на доступ и сразу же тестируем
их на целевой странице. Побеждает первый, кто реально открывает игру.
"""
import concurrent.futures as cf
import json
import os
import re
import socket
import sys
import time
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

PROXY_SOURCES = (
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt",
)
IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def get(url: str, timeout: int = 10, proxy: str = "") -> bytes:
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({
            "http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def candidates(limit: int = 40) -> list:
    out, seen = [], set()
    for src in PROXY_SOURCES:
        try:
            body = get(src, timeout=12).decode("utf-8", "replace")
        except Exception:                                 # noqa: BLE001
            continue
        for line in body.split("\n"):
            line = line.strip()
            if not line or line in seen:
                continue
            if "://" in line:
                scheme, _, hostport = line.partition("://")
                if scheme.lower() not in ("http", "https", "socks4", "socks5"):
                    continue
                cand = scheme.lower() + "://" + hostport
            else:
                hostport = line
                cand = "http://" + hostport
            host = cand.split("//", 1)[1].split(":")[0]
            if not IP_RE.match(host):
                continue
            seen.add(hostport)
            out.append(cand)
            if len(out) >= limit:
                return out
    return out


def check(proxy: str, target: str, timeout: int = 8) -> tuple:
    """Прокси годен, если через него открывается целевая страница."""
    try:
        body = get(target, timeout=timeout, proxy=proxy)
        if not body:
            return False, 0
        try:
            return True, len(body)
        except Exception:                                 # noqa: BLE001
            return True, len(body)
    except urllib.error.HTTPError as e:
        return (e.code not in (403, 407, 502, 503) and e.code != 407), 0
    except Exception:                                     # noqa: BLE001
        return False, 0


def pick(target: str, limit: int = 40, workers: int = 24, cache: str = "") -> str:
    cands = candidates(limit)
    print("прокси-кандидатов: %d" % len(cands), flush=True)
    if not cands:
        return ""
    best = ""
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(check, c, target): c for c in cands}
        for fut in cf.as_completed(futs, timeout=70):
            cand = futs[fut]
            try:
                ok, size = fut.result()
            except Exception:                             # noqa: BLE001
                continue
            if ok and size > 20000:
                print("прокси работает: %s (%d Б)" % (cand, size), flush=True)
                best = cand
                break
    if not best:
        print("ни один публичный прокси не подошёл", flush=True)
    return best


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "https://api.ipify.org?format=json"
    print(pick(target))
