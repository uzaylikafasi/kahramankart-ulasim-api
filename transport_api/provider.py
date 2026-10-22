from __future__ import annotations

import asyncio
import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://maras.pallium.com.tr/PalliumMarasServices/Service.svc/"
ROOT = Path(__file__).resolve().parent.parent


class ProviderError(Exception):
    def __init__(self, code: str, message: str, status: int = 502):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def load_authorization() -> str:
    if value := os.getenv("PALLIUM_AUTHORIZATION"):
        return value.strip()
    path = Path(os.getenv("PALLIUM_AUTH_FILE", str(ROOT / "analysis/transport-auth.json")))
    try:
        content = json.loads(path.read_text())
        value = next(v for k, v in content.items() if k.lower() == "authorization")
        if not isinstance(value, str) or not value.strip():
            raise ValueError()
        return value.strip()
    except (OSError, ValueError, StopIteration, AttributeError, TypeError) as exc:
        raise RuntimeError("Pallium erişim bilgisi bulunamadı. PALLIUM_AUTHORIZATION veya PALLIUM_AUTH_FILE ayarlayın.") from exc


@dataclass(frozen=True)
class Result:
    data: dict[str, Any]
    fetched_at: str
    cached: bool = False


class Provider:
    def __init__(self, client: httpx.AsyncClient, max_entries: int = 256):
        self.client = client
        self.max_entries = max_entries
        self.cache: OrderedDict[tuple, tuple[float, Result]] = OrderedDict()
        self.pending: dict[tuple, asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(4)

    async def close(self):
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.aclose()

    async def get(self, operation: str, params: dict | None = None, ttl: int = 60) -> Result:
        return await self._request("GET", operation, params or {}, ttl)

    async def post(self, operation: str, body: dict, ttl: int = 60) -> Result:
        return await self._request("POST", operation, body, ttl)

    async def _request(self, method: str, operation: str, params: dict, ttl: int) -> Result:
        params = params or {}
        key = (method, operation, json.dumps(params, sort_keys=True))
        now = time.monotonic()
        if key in self.cache:
            expires, result = self.cache[key]
            if expires > now:
                self.cache.move_to_end(key)
                return Result(result.data, result.fetched_at, cached=True)
            del self.cache[key]
        task = self.pending.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch(method, operation, params, key, ttl))
            self.pending[key] = task

            def done(completed):
                self.pending.pop(key, None)
                # Retrieve exceptions even if the HTTP caller disconnected.
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(done)
        return await asyncio.shield(task)

    async def _fetch(self, method, operation, params, key, ttl):
        try:
            async with self.semaphore:
                response = await self.client.request(method, operation,
                    **({"params": params} if method == "GET" else {"json": params}))
        except httpx.TimeoutException:
            raise ProviderError("UPSTREAM_TIMEOUT", "Ulaşım servisi zamanında yanıt vermedi.", 504) from None
        except httpx.RequestError:
            raise ProviderError("UPSTREAM_UNAVAILABLE", "Ulaşım servisine bağlanılamadı.") from None
        if response.status_code in (401, 403):
            raise ProviderError("UPSTREAM_AUTH_FAILED", "Ulaşım servisinin yerel erişim bilgisini güncelleyin.")
        if response.status_code == 429:
            raise ProviderError("UPSTREAM_RATE_LIMITED", "Ulaşım servisi istek sınırı uyguluyor.", 503)
        if response.status_code != 200:
            raise ProviderError("UPSTREAM_HTTP_ERROR", f"Ulaşım servisi HTTP {response.status_code} döndürdü.")
        try:
            data = response.json()
        except ValueError:
            raise ProviderError("UPSTREAM_INVALID_RESPONSE", "Ulaşım servisi geçerli JSON döndürmedi.") from None
        if not isinstance(data, dict):
            raise ProviderError("UPSTREAM_INVALID_RESPONSE", "Ulaşım servisinin yanıt biçimi değişmiş.")
        if "processInfo" in data and str(data["processInfo"]) != "1":
            raise ProviderError("UPSTREAM_APPLICATION_ERROR", "Ulaşım servisi sorguyu tamamlayamadı.")
        containers = [data]
        if isinstance(data.get("GetJourneyPointResult"), dict):
            containers.append(data["GetJourneyPointResult"])
        for container in containers:
            for error_key, code_key in (("Error", "Code"), ("error", "code")):
                if isinstance(container.get(error_key), dict) and container[error_key].get(code_key) not in (None, 0, "0"):
                    raise ProviderError("UPSTREAM_APPLICATION_ERROR", "Ulaşım servisi sorguyu tamamlayamadı.")
        result = Result(data, datetime.now(timezone.utc).isoformat())
        if ttl > 0:
            self.cache[key] = (time.monotonic() + ttl, result)
            self.cache.move_to_end(key)
            while len(self.cache) > self.max_entries:
                self.cache.popitem(last=False)
        return result


def create_provider() -> Provider:
    return Provider(httpx.AsyncClient(
        base_url=BASE_URL,
        headers={"Authorization": load_authorization(), "Accept": "application/json"},
        timeout=httpx.Timeout(20, connect=5, pool=5),
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        follow_redirects=False,
        trust_env=False,
    ))
