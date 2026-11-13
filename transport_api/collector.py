"""Çevrimdışı model için canlı araç konumu toplayıcı (API süreci içinde, isteğe bağlı).

COLLECTOR_ENABLED=1 olmadıkça çalışmaz. Sağlayıcıyı doğrudan çağırır (genel vekil ve onun sınırları
devreye girmez) ve ttl=0 kullanır (kullanıcı önbelleğine kayıt eklemez). Kayıt biçimi:
docs/offline-model-sema.md "Ham kayıt biçimi". Kayıtlar bellekte biriktirilir ve birkaç dakikada bir
data/anlik/YYYY-MM-DD.jsonl.gz dosyasına ayrı bir gzip üyesi olarak eklenir (SD kart yıpranmasın).
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import random
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .provider import ROOT, ProviderError

TZ = ZoneInfo("Europe/Istanbul")
log = logging.getLogger("uvicorn.error")


def data_dir() -> Path:
    return Path(os.getenv("MARAS_DATA_DIR", str(ROOT / "data")))


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(low, min(high, value))


def _env_time(name: str, default: str) -> time:
    try:
        return time.fromisoformat(os.getenv(name, default))
    except ValueError:
        return time.fromisoformat(default)


@dataclass
class CollectorConfig:
    data_dir: Path = field(default_factory=data_dir)
    active_seconds: int = 240      # araçlı hat yönü sorgu aralığı
    idle_seconds: int = 600        # araçsız hat yönü sorgu aralığı
    tick_seconds: int = 60         # zamanı gelen hatların kontrol edildiği tur aralığı
    concurrency: int = 2           # sağlayıcının Semaphore(4) değerinden küçük kalmalı
    flush_seconds: int = 300
    retention_days: int = 60
    window_start: time = time(5, 30)
    window_end: time = time(0, 30)
    max_failures: int = 3
    max_backoff_seconds: int = 1800
    max_buffer: int = 20000

    @classmethod
    def from_env(cls) -> "CollectorConfig":
        active = _env_int("COLLECTOR_ACTIVE_SECONDS", 240, 120, 3600)
        return cls(
            data_dir=data_dir(),
            active_seconds=active,
            idle_seconds=_env_int("COLLECTOR_IDLE_SECONDS", 600, active, 7200),
            tick_seconds=_env_int("COLLECTOR_TICK_SECONDS", 60, 15, 600),
            concurrency=_env_int("COLLECTOR_CONCURRENCY", 2, 1, 3),
            flush_seconds=_env_int("COLLECTOR_FLUSH_SECONDS", 300, 30, 3600),
            retention_days=_env_int("COLLECTOR_RETENTION_DAYS", 60, 1, 3650),
            window_start=_env_time("COLLECTOR_WINDOW_START", "05:30"),
            window_end=_env_time("COLLECTOR_WINDOW_END", "00:30"),
        )

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "anlik"

    @property
    def static_dir(self) -> Path:
        return self.data_dir / "statik"

    @property
    def status_path(self) -> Path:
        return self.data_dir / "collector-status.json"


# ------------------------------------------------------------------ saf parçalar
def in_window(moment: datetime, start: time, end: time) -> bool:
    """Toplama penceresinde mi (yerel saat). start > end ise pencere gece yarısını aşar."""
    local = moment.astimezone(TZ).time().replace(tzinfo=None)
    if start <= end:
        return start <= local < end
    return local >= start or local < end


def seconds_until_window(moment: datetime, start: time, end: time) -> float:
    if in_window(moment, start, end):
        return 0.0
    local = moment.astimezone(TZ)
    target = datetime.combine(local.date(), start, TZ)
    if target <= local:
        target += timedelta(days=1)
    return (target - local).total_seconds()


def backoff_seconds(level: int, base: int, cap: int) -> int:
    """Art arda başarısız turlar için üstel bekleme: base, 2*base, 4*base ... en çok cap."""
    return min(cap, base * (2 ** max(0, level - 1))) if level > 0 else 0


def build_record(round_start: str, fetched_at: str, line_id: int, code: str, direction: int,
                 data: dict[str, Any]) -> dict[str, Any]:
    info = data.get("busLineInfo")
    buses = info.get("liveBuses") if isinstance(info, dict) else None
    if not isinstance(buses, list):
        raise ProviderError("UPSTREAM_SCHEMA_CHANGED", "Canlı araç yanıtının biçimi değişmiş.")
    vehicles = []
    for bus in buses:
        if not isinstance(bus, dict):
            continue
        loc = bus.get("location") if isinstance(bus.get("location"), dict) else {}
        vehicles.append({"p": bus.get("plate") or bus.get("busNo"), "la": loc.get("latitude"),
                         "lo": loc.get("longitude"), "s": bus.get("nextStopId"), "o": bus.get("previousStopId"),
                         "h": bus.get("speed"), "a": bus.get("directionAngle"), "k": bus.get("timeToNearestStop")})
    return {"tur": round_start, "t": fetched_at, "h": line_id, "k": code, "y": direction, "v": vehicles}


class Schedule:
    """Uyarlanır sıklık: son sorguda aracı olan hat yönü active, olmayan idle saniyede bir sorgulanır."""

    def __init__(self, active_seconds: int, idle_seconds: int):
        self.active_seconds, self.idle_seconds = active_seconds, idle_seconds
        self.next_due: dict[Any, float] = {}
        self.active: dict[Any, bool] = {}

    def sync(self, keys: list, now: float, rng: random.Random | None = None) -> None:
        """Yeni hatları ilk aralığa yayarak ekle, listeden çıkanları unut."""
        rng = rng or random.Random(0)
        wanted = set(keys)
        for key in list(self.next_due):
            if key not in wanted:
                self.next_due.pop(key, None)
                self.active.pop(key, None)
        for key in keys:
            if key not in self.next_due:
                self.next_due[key] = now + rng.uniform(0, self.active_seconds)

    def due(self, now: float) -> list:
        return sorted((k for k, t in self.next_due.items() if t <= now), key=lambda k: self.next_due[k])

    def record(self, key, now: float, had_vehicles: bool | None) -> None:
        """had_vehicles None: sorgu başarısız; önceki durum korunur, aralığı kadar sonra yeniden denenir."""
        if had_vehicles is not None:
            self.active[key] = had_vehicles
        active = self.active.get(key, False)
        self.next_due[key] = now + (self.active_seconds if active else self.idle_seconds)

    def active_count(self) -> int:
        return sum(1 for v in self.active.values() if v)


def append_gzip(path: Path, lines: list[str]) -> None:
    """Satırları dosyaya yeni bir gzip üyesi olarak ekle (art arda üyeler geçerli gzip'tir)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = gzip.compress(("\n".join(lines) + "\n").encode("utf-8"), 6)
    with open(path, "ab") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())


def prune_raw(raw_dir: Path, today: date, keep_days: int) -> list[Path]:
    removed = []
    limit = today - timedelta(days=keep_days)
    for p in raw_dir.glob("*.jsonl.gz"):
        try:
            day = date.fromisoformat(p.name.split(".")[0])
        except ValueError:
            continue
        if day < limit:
            p.unlink(missing_ok=True)
            removed.append(p)
    return removed


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_lines(static_dir: Path) -> list[tuple[int, int, str]]:
    """hatlar.json -> [(hatId, yön, kod)]."""
    raw = json.loads((static_dir / "hatlar.json").read_text(encoding="utf-8"))
    out = []
    for hid, h in raw.items():
        try:
            out.append((int(hid), int(h["yon"]), str(h["kod"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out)


class AuthStopped(Exception):
    pass


# ------------------------------------------------------------------ toplayıcı
class Collector:
    def __init__(self, provider, config: CollectorConfig | None = None,
                 clock: Callable[[], float] = _time.time, sleep=asyncio.sleep):
        self.provider = provider
        self.config = config or CollectorConfig.from_env()
        self.clock, self.sleep = clock, sleep
        self.schedule = Schedule(self.config.active_seconds, self.config.idle_seconds)
        self.lines: dict[tuple[int, int], str] = {}
        self._lines_mtime: float | None = None
        self.buffer: list[tuple[str, str]] = []  # (yerel tarih, json satırı)
        self.last_flush = self.clock()
        self.failed_rounds = 0
        self.backoff_until = 0.0
        self.last_pruned: date | None = None
        self.stats = {"state": "starting", "started_at": self._iso(self.clock()), "rounds": 0, "polls": 0,
                      "failures": 0, "abandoned_rounds": 0, "records_written": 0, "last_round": None,
                      "last_round_polled": 0, "last_round_vehicles": 0, "active_lines": 0, "lines": 0,
                      "last_flush": None, "last_error": None}

    @staticmethod
    def _iso(ts: float, tz=TZ) -> str:
        return datetime.fromtimestamp(ts, tz).isoformat(timespec="seconds")

    def refresh_lines(self) -> None:
        path = self.config.static_dir / "hatlar.json"
        mtime = path.stat().st_mtime
        if mtime == self._lines_mtime:
            return
        lines = load_lines(self.config.static_dir)
        if not lines:
            raise ValueError("hatlar.json boş")
        self.lines = {(hid, d): code for hid, d, code in lines}
        self._lines_mtime = mtime
        self.schedule.sync(sorted(self.lines), self.clock())
        self.stats["lines"] = len(self.lines)

    async def run_round(self) -> dict[str, int]:
        """Zamanı gelen hat yönlerini sorgula. AuthStopped yükseltebilir."""
        now = self.clock()
        due = self.schedule.due(now)
        round_start = self._iso(now)
        sem = asyncio.Semaphore(self.config.concurrency)
        state = {"consecutive": 0, "abandon": False, "auth": False, "polled": 0, "failed": 0, "vehicles": 0}

        async def poll(key):
            async with sem:
                if state["abandon"]:
                    return
                hid, direction = key
                try:
                    result = await self.provider.get("BusLine", {"lineId": hid, "direction": direction}, ttl=0)
                    record = build_record(round_start, result.fetched_at, hid, self.lines[key], direction, result.data)
                except ProviderError as exc:
                    state["failed"] += 1
                    state["consecutive"] += 1
                    self.stats["last_error"] = f"{exc.code} ({self._iso(self.clock())})"
                    self.schedule.record(key, self.clock(), None)
                    if exc.code == "UPSTREAM_AUTH_FAILED":
                        state["auth"] = state["abandon"] = True
                    elif state["consecutive"] >= self.config.max_failures:
                        state["abandon"] = True
                    return
                state["consecutive"] = 0
                state["polled"] += 1
                state["vehicles"] += len(record["v"])
                self.schedule.record(key, self.clock(), bool(record["v"]))
                local_day = datetime.fromtimestamp(now, TZ).date().isoformat()
                self.buffer.append((local_day, json.dumps(record, ensure_ascii=False, separators=(",", ":"))))

        await asyncio.gather(*(poll(k) for k in due))
        if len(self.buffer) > self.config.max_buffer:
            del self.buffer[:len(self.buffer) - self.config.max_buffer]
        s = self.stats
        s["rounds"] += 1
        s["polls"] += state["polled"]
        s["failures"] += state["failed"]
        s["last_round"], s["last_round_polled"], s["last_round_vehicles"] = round_start, state["polled"], state["vehicles"]
        s["active_lines"] = self.schedule.active_count()
        if state["auth"]:
            raise AuthStopped()
        if state["abandon"]:
            s["abandoned_rounds"] += 1
            self.failed_rounds += 1
            wait = backoff_seconds(self.failed_rounds, self.config.tick_seconds * 2, self.config.max_backoff_seconds)
            self.backoff_until = self.clock() + wait
            log.warning("Toplayıcı: %d art arda hata, tur bırakıldı; %d sn bekleniyor (%s)",
                        self.config.max_failures, wait, s["last_error"])
        elif state["polled"]:
            self.failed_rounds = 0
        return {"due": len(due), "polled": state["polled"], "failed": state["failed"], "vehicles": state["vehicles"]}

    def flush(self, state: str | None = None) -> None:
        """Tamponu günlük dosyalara yaz, durum dosyasını güncelle, eski ham dosyaları sil (senkron)."""
        if state:
            self.stats["state"] = state
        now = self.clock()
        buffer, self.buffer = self.buffer, []
        by_day: dict[str, list[str]] = {}
        for day, line in buffer:
            by_day.setdefault(day, []).append(line)
        try:
            for day, lines in by_day.items():
                append_gzip(self.config.raw_dir / f"{day}.jsonl.gz", lines)
                self.stats["records_written"] += len(lines)
        except OSError as exc:
            log.error("Toplayıcı: ham veri yazılamadı: %s", exc)
            self.stats["last_error"] = f"WRITE_FAILED ({self._iso(now)})"
            self.buffer = buffer + self.buffer
        self.last_flush = now
        self.stats["last_flush"] = self._iso(now)
        today = datetime.fromtimestamp(now, TZ).date()
        if self.last_pruned != today:
            self.last_pruned = today
            try:
                for p in prune_raw(self.config.raw_dir, today, self.config.retention_days):
                    log.info("Toplayıcı: eski ham dosya silindi: %s", p.name)
            except OSError as exc:
                log.error("Toplayıcı: eski dosyalar silinemedi: %s", exc)
        try:
            write_json_atomic(self.config.status_path, self.stats)
        except OSError as exc:
            log.error("Toplayıcı: durum dosyası yazılamadı: %s", exc)
        s = self.stats
        log.info("Toplayıcı: durum=%s tur=%d sorgu=%d hata=%d aktif_hat=%d/%d yazilan=%d",
                 s["state"], s["rounds"], s["polls"], s["failures"], s["active_lines"], s["lines"], s["records_written"])

    async def run(self) -> None:
        cfg = self.config
        log.info("Toplayıcı başladı: aktif %d sn, boş %d sn, eşzamanlı %d, pencere %s-%s",
                 cfg.active_seconds, cfg.idle_seconds, cfg.concurrency, cfg.window_start, cfg.window_end)
        try:
            while True:
                now = self.clock()
                wait = seconds_until_window(datetime.fromtimestamp(now, timezone.utc), cfg.window_start, cfg.window_end)
                if wait > 0:
                    if self.stats["state"] != "night":
                        self.flush("night")
                    await self.sleep(min(wait, 600))
                    continue
                if now < self.backoff_until:
                    self.stats["state"] = "backoff"
                    await self.sleep(min(self.backoff_until - now, cfg.tick_seconds))
                    continue
                try:
                    self.refresh_lines()
                except (OSError, ValueError) as exc:
                    self.stats["last_error"] = f"STATIC_UNAVAILABLE ({self._iso(now)})"
                    log.error("Toplayıcı: hat listesi okunamadı: %s", exc)
                    await self.sleep(cfg.idle_seconds)
                    continue
                self.stats["state"] = "running"
                try:
                    await self.run_round()
                except AuthStopped:
                    log.error("Toplayıcı DURDU: Pallium erişim bilgisi reddedildi (UPSTREAM_AUTH_FAILED). "
                              "Bilgiyi güncelleyip servisi yeniden başlatın; otomatik yeniden deneme yok.")
                    self.flush("stopped_auth")
                    return
                if self.clock() - self.last_flush >= cfg.flush_seconds:
                    self.flush()
                elapsed = self.clock() - now
                await self.sleep(max(1.0, cfg.tick_seconds - elapsed))
        except asyncio.CancelledError:
            self.flush("stopped")
            raise
        except Exception:
            log.exception("Toplayıcı beklenmeyen hatayla durdu")
            self.flush("crashed")
            raise
