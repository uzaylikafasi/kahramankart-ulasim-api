"""Narrow GET-only bridge from mithrixa.com/ulasim-api to the local API.

Loaded by the existing bio HTTP handler; no credentials or arbitrary target URLs.
"""
import http.client
import json
import re
import threading
import time
from collections import deque
from pathlib import Path
import shutil

PREFIX = "/ulasim-api"
ROOT = Path(__file__).resolve().parents[1]
APK_NAME = "maras-ulasim-v1.0.0.apk"
APK_PATH = ROOT / "dist" / APK_NAME
# Herkese açık, sabit belgeler: doküman sayfası ve API sözleşmesi
STATIC = {"/docs": (ROOT / "deployment/api-docs.html", "text/html; charset=utf-8"),
          "/openapi.json": (ROOT / "openapi.json", "application/json; charset=utf-8")}
TOTAL_PER_MINUTE = 480
IP_PER_MINUTE = 90
_PATH = re.compile(r"/(?:healthz|v1/(?:capabilities|search|announcements|journey-points|journeys|timetables|stops/(?:nearby|in-bounds|[1-9][0-9]*(?:/(?:lines|arrivals))?)|lines/[1-9][0-9]*(?:/(?:vehicles|stops|shape))?))")
_slots = threading.BoundedSemaphore(8)
_lock = threading.Lock()
_requests = deque()
_per_ip = {}


def _reply(handler, status, body, retry=False):
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    if retry:
        handler.send_header("Retry-After", "15")
    handler.end_headers()
    handler.wfile.write(body)


def _client_ip(handler):
    # Cloudflare tüneli gerçek istemci adresini bu başlıkta verir
    ip = handler.headers.get("CF-Connecting-IP", "") or (getattr(handler, "client_address", None) or ("?",))[0]
    return ip[:64]


def _allowed(ip):
    with _lock:
        now = time.monotonic()
        while _requests and _requests[0] <= now - 60:
            _requests.popleft()
        if len(_per_ip) > 5000:
            for key in [k for k, v in _per_ip.items() if not v or v[-1] <= now - 60]:
                del _per_ip[key]
        mine = _per_ip.setdefault(ip, deque())
        while mine and mine[0] <= now - 60:
            mine.popleft()
        if len(_requests) >= TOTAL_PER_MINUTE or len(mine) >= IP_PER_MINUTE:
            return False
        _requests.append(now)
        mine.append(now)
        return True


def _send_file(handler, path, content_type, filename=None):
    try:
        with path.open("rb") as source:
            handler.send_response(200)
            handler.send_header("Content-Type", content_type)
            if filename:
                handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            handler.send_header("Cache-Control", "no-store" if filename else "public, max-age=300")
            handler.send_header("Content-Length", str(path.stat().st_size))
            handler.end_headers()
            shutil.copyfileobj(source, handler.wfile, length=64 * 1024)
    except FileNotFoundError:
        _reply(handler, 503, b'{"error":{"code":"FILE_UNAVAILABLE"}}')


def proxy_transport(handler):
    path, _, query = handler.path.partition("?")
    if path != PREFIX and not path.startswith(PREFIX + "/"):
        return False
    target = path[len(PREFIX):]
    if target in ("", "/"):
        target = "/docs"
    download = target == "/download" and not query
    static = STATIC.get(target) if not query else None
    if handler.command != "GET" or (not _PATH.fullmatch(target) and not download and not static) or len(query) > 4096:
        _reply(handler, 404, b'{"error":{"code":"NOT_FOUND"}}')
        return True
    if not _allowed(_client_ip(handler)) or not _slots.acquire(blocking=False):
        _reply(handler, 429, b'{"error":{"code":"RATE_LIMITED"}}', retry=True)
        return True
    if download or static:
        try:
            if download:
                _send_file(handler, APK_PATH, "application/vnd.android.package-archive", APK_NAME)
            else:
                _send_file(handler, *static)
        finally:
            _slots.release()
        return True
    connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=25)
    try:
        headers = {"Accept": "application/json"}
        key = handler.headers.get("X-API-Key", "")
        if key and len(key) <= 256 and all(32 <= ord(c) <= 126 for c in key):
            headers["X-API-Key"] = key
        connection.request("GET", target + ("?" + query if query else ""), headers=headers)
        response = connection.getresponse()
        body = response.read(4_000_001)
        if len(body) > 4_000_000 or "application/json" not in response.getheader("Content-Type", ""):
            raise ValueError("Unexpected local API response")
        json.loads(body)
        status = response.status
    except (OSError, ValueError, http.client.HTTPException):
        status, body = 502, b'{"error":{"code":"API_UNAVAILABLE"}}'
    finally:
        connection.close()
        _slots.release()
    _reply(handler, status, body)
    return True
