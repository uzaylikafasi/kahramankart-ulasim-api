"""Toplayıcı, statik yenileme, model yenileme ve çevrimdışı model uçları. Gerçek Pallium'a istek yok."""
import asyncio
import gzip
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from deployment import transport_proxy as proxy
from transport_api import collector as col
from transport_api import static_refresh as sr
from transport_api.main import create_app
from transport_api.provider import BASE_URL, Provider, ProviderError, Result

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Europe/Istanbul")
sys.path.insert(0, str(ROOT / "tools"))
import model_yenile  # noqa: E402


def local(h, m, day=13):
    return datetime(2026, 11, day, h, m, tzinfo=TZ)


BUS = {"busNo": "46 H 0200", "plate": "46 H 0200", "directionAngle": 291,
       "location": {"latitude": 37.55127, "longitude": 36.92057}, "nextStopId": "6378",
       "previousStopId": "6935", "speed": 12.5, "timeToNearestStop": 0, "nextStopName": "X"}


class FakeProvider:
    """Sağlayıcı yerine geçer; yanıtı fn(operation, params) üretir, çağrıları kaydeder."""

    def __init__(self, fn):
        self.fn, self.calls, self.inflight, self.max_inflight = fn, [], 0, 0

    async def get(self, operation, params=None, ttl=60):
        self.calls.append((operation, dict(params or {}), ttl))
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(0)
            value = self.fn(operation, params or {})
            if isinstance(value, Exception):
                raise value
            return Result(value, "2026-11-13T07:00:00+00:00")
        finally:
            self.inflight -= 1


class Clock:
    def __init__(self, ts):
        self.ts = ts

    def __call__(self):
        return self.ts


def write_static(sdir, lines):
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "hatlar.json").write_text(json.dumps(
        {str(h): {"kod": k, "yon": d, "ad": f"{k} (Gidiş)"} for h, d, k in lines}), encoding="utf-8")


# ------------------------------------------------------------------ saf parçalar
@pytest.mark.parametrize("moment,inside", [
    (local(5, 29), False), (local(5, 30), True), (local(12, 0), True), (local(23, 59), True),
    (local(0, 29), True), (local(0, 30), False), (local(3, 0), False)])
def test_night_window_crosses_midnight(moment, inside):
    assert col.in_window(moment, time(5, 30), time(0, 30)) is inside
    assert col.in_window(moment.astimezone(timezone.utc), time(5, 30), time(0, 30)) is inside


def test_seconds_until_window():
    assert col.seconds_until_window(local(4, 30), time(5, 30), time(0, 30)) == 3600
    assert col.seconds_until_window(local(0, 30), time(5, 30), time(0, 30)) == 5 * 3600
    assert col.seconds_until_window(local(9, 0), time(5, 30), time(0, 30)) == 0


def test_backoff_is_exponential_and_capped():
    assert [col.backoff_seconds(n, 120, 1800) for n in range(6)] == [0, 120, 240, 480, 960, 1800]


def test_record_matches_raw_schema():
    rec = col.build_record("2026-11-13T10:00:00+03:00", "2026-11-13T07:00:01+00:00", 161, "H/1", 2,
                           {"busLineInfo": {"liveBuses": [BUS, "bozuk"]}})
    assert rec == {"tur": "2026-11-13T10:00:00+03:00", "t": "2026-11-13T07:00:01+00:00", "h": 161, "k": "H/1", "y": 2,
                   "v": [{"p": "46 H 0200", "la": 37.55127, "lo": 36.92057, "s": "6378", "o": "6935",
                          "h": 12.5, "a": 291, "k": 0}]}


@pytest.mark.parametrize("data", [{}, {"busLineInfo": None}, {"busLineInfo": {"liveBuses": None}}])
def test_record_rejects_changed_schema(data):
    with pytest.raises(ProviderError):
        col.build_record("t", "t", 1, "X", 1, data)


def test_adaptive_schedule():
    s = col.Schedule(240, 600)
    s.sync([(1, 1), (2, 1)], now=0)
    assert all(0 <= t <= 240 for t in s.next_due.values())
    assert set(s.due(240)) == {(1, 1), (2, 1)}
    s.record((1, 1), 240, True)
    s.record((2, 1), 240, False)
    assert s.due(479) == [] and s.due(480) == [(1, 1)]
    assert set(s.due(840)) == {(1, 1), (2, 1)}
    s.record((1, 1), 480, None)  # başarısız: durum korunur (aktif), aktif aralıkla yeniden
    assert s.next_due[(1, 1)] == 720 and s.active_count() == 1
    s.sync([(2, 1)], now=500)
    assert set(s.next_due) == {(2, 1)}


def test_append_gzip_members_and_prune(tmp_path):
    path = tmp_path / "2026-11-13.jsonl.gz"
    col.append_gzip(path, ['{"a":1}'])
    col.append_gzip(path, ['{"a":2}', '{"a":3}'])
    with gzip.open(path, "rt") as f:
        assert [json.loads(x)["a"] for x in f] == [1, 2, 3]
    old = tmp_path / "2026-09-01.jsonl.gz"
    old.write_bytes(b"")
    (tmp_path / "notlar.jsonl.gz").write_bytes(b"")
    assert col.prune_raw(tmp_path, date(2026, 11, 13), 60) == [old]
    assert path.exists() and (tmp_path / "notlar.jsonl.gz").exists()


def test_config_clamps_unsafe_values(monkeypatch):
    monkeypatch.setenv("COLLECTOR_ACTIVE_SECONDS", "10")
    monkeypatch.setenv("COLLECTOR_CONCURRENCY", "8")
    monkeypatch.setenv("COLLECTOR_IDLE_SECONDS", "x")
    cfg = col.CollectorConfig.from_env()
    assert cfg.active_seconds == 120 and cfg.concurrency == 3 and cfg.idle_seconds == 600
    monkeypatch.delenv("COLLECTOR_ACTIVE_SECONDS")
    monkeypatch.delenv("COLLECTOR_CONCURRENCY")
    cfg = col.CollectorConfig.from_env()
    assert (cfg.active_seconds, cfg.idle_seconds, cfg.concurrency) == (240, 600, 2)


# ------------------------------------------------------------------ toplayıcı turları
def make_collector(tmp_path, fn, lines=((161, 2, "H/1"), (501, 3, "B/501"), (9589, 2, "B/81"))):
    write_static(tmp_path / "statik", lines)
    provider = FakeProvider(fn)
    clock = Clock(local(10, 0).timestamp())
    c = col.Collector(provider, col.CollectorConfig(data_dir=tmp_path), clock=clock)
    c.refresh_lines()
    for k in c.schedule.next_due:
        c.schedule.next_due[k] = clock.ts
    return c, provider, clock


def test_round_uses_provider_directly_without_cache_and_limited_concurrency(tmp_path):
    def fn(op, params):
        buses = [BUS] if params["lineId"] == 161 else []
        return {"busLineInfo": {"liveBuses": buses}}

    c, provider, clock = make_collector(tmp_path, fn)
    out = asyncio.run(c.run_round())
    assert out == {"due": 3, "polled": 3, "failed": 0, "vehicles": 1}
    assert {(op, ttl) for op, _, ttl in provider.calls} == {("BusLine", 0)}
    assert {(p["lineId"], p["direction"]) for _, p, _ in provider.calls} == {(161, 2), (501, 3), (9589, 2)}
    assert provider.max_inflight <= 2
    # Aracı olan hat 4 dk, olmayanlar 10 dk sonra
    assert c.schedule.next_due[(161, 2)] == clock.ts + 240
    assert c.schedule.next_due[(501, 3)] == clock.ts + 600
    c.flush()
    with gzip.open(tmp_path / "anlik" / "2026-11-13.jsonl.gz", "rt") as f:
        recs = [json.loads(x) for x in f]
    assert len(recs) == 3 and recs[0]["tur"] == "2026-11-13T10:00:00+03:00"
    assert next(r for r in recs if r["h"] == 161)["v"][0]["p"] == "46 H 0200"
    status = json.loads((tmp_path / "collector-status.json").read_text())
    assert status["rounds"] == 1 and status["polls"] == 3 and status["records_written"] == 3


def test_three_consecutive_failures_abandon_round_and_back_off(tmp_path):
    lines = [(i, 1, f"L{i}") for i in range(1, 11)]
    c, provider, clock = make_collector(tmp_path, lambda op, p: ProviderError("UPSTREAM_TIMEOUT", "x", 504), lines)
    out = asyncio.run(c.run_round())
    # 3. hatada tur bırakılır; eşzamanlılık 2 olduğundan uçuştaki en çok bir istek daha biter
    assert 3 <= out["failed"] <= 4 and len(provider.calls) <= 4
    assert c.backoff_until == clock.ts + 120
    for k in c.schedule.next_due:
        c.schedule.next_due[k] = clock.ts
    asyncio.run(c.run_round())
    assert c.backoff_until == clock.ts + 240 and c.stats["abandoned_rounds"] == 2


def test_auth_failure_stops_collector_without_retry(tmp_path):
    c, provider, clock = make_collector(tmp_path, lambda op, p: ProviderError("UPSTREAM_AUTH_FAILED", "x"))
    sleeps = []

    async def sleep(s):
        sleeps.append(s)

    c.sleep = sleep
    asyncio.run(c.run())  # döngü kendiliğinden biter
    assert len(provider.calls) <= 2 and sleeps == []
    assert json.loads((tmp_path / "collector-status.json").read_text())["state"] == "stopped_auth"


def test_night_pause_makes_no_requests(tmp_path):
    c, provider, clock = make_collector(tmp_path, lambda op, p: {"busLineInfo": {"liveBuses": []}})
    clock.ts = local(2, 0).timestamp()
    sleeps = []

    async def sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError()

    c.sleep = sleep
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(c.run())
    assert provider.calls == [] and sleeps[0] == 600
    assert json.loads((tmp_path / "collector-status.json").read_text())["state"] == "stopped"


def test_collector_is_off_by_default_and_cancelled_on_shutdown(tmp_path, monkeypatch):
    started = []

    class Dummy:
        def __init__(self, provider, config):
            started.append(provider)

        async def run(self):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                started.append("cancelled")
                raise

    import transport_api.main as main
    monkeypatch.setattr(main, "Collector", Dummy)
    app = create_app(lambda: Provider(httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(lambda r: None))))
    monkeypatch.delenv("COLLECTOR_ENABLED", raising=False)
    with TestClient(app):
        pass
    assert started == []
    monkeypatch.setenv("COLLECTOR_ENABLED", "1")
    with TestClient(app):
        assert len(started) == 1
    assert started[-1] == "cancelled"


# ------------------------------------------------------------------ uçlar
def test_meta_endpoint_serves_envelope_and_404_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("MARAS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("COLLECTOR_ENABLED", raising=False)
    app = create_app(lambda: Provider(httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(lambda r: None))))
    with TestClient(app) as client:
        r = client.get("/v1/offline-model/meta")
        assert r.status_code == 404 and r.json()["error"]["code"] == "OFFLINE_MODEL_UNAVAILABLE"
        meta = {"schema": 1, "model_version": "20261113-aa", "sha256": "0" * 64, "built_at": "2026-11-13T00:30:00Z"}
        (tmp_path / "model-meta.json").write_text(json.dumps(meta))
        r = client.get("/v1/offline-model/meta")
        assert r.status_code == 200 and r.json()["data"] == meta and r.json()["meta"]["cached"] is False
        (tmp_path / "model-meta.json").write_text("{bozuk")
        assert client.get("/v1/offline-model/meta").status_code == 404


def test_meta_endpoint_respects_access_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MARAS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_ACCESS_KEY", "gizli")
    app = create_app(lambda: Provider(httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(lambda r: None))))
    with TestClient(app) as client:
        assert client.get("/v1/offline-model/meta").status_code == 401


class Handler:
    command = "GET"
    headers = {}

    def __init__(self, path):
        import io
        self.path, self.wfile, self.response_headers = path, io.BytesIO(), {}

    def send_response(self, code):
        self.status = code

    def send_header(self, name, value):
        self.response_headers[name] = value

    def end_headers(self):
        pass


def test_proxy_forwards_meta_and_serves_model_file(tmp_path):
    from unittest.mock import patch
    proxy._requests.clear(); proxy._per_ip.clear()
    assert proxy._PATH.fullmatch("/v1/offline-model/meta")
    assert not proxy._PATH.fullmatch("/v1/offline-model/../meta")
    model = tmp_path / "offline-model.json.gz"
    model.write_bytes(gzip.compress(b"{}" * 10, mtime=0))
    entry = (model, "application/gzip")
    with patch.dict(proxy.STATIC, {"/offline-model": entry}), patch.object(proxy.http.client, "HTTPConnection") as conn:
        h = Handler("/ulasim-api/offline-model")
        assert proxy.proxy_transport(h)
        conn.assert_not_called()
        assert h.status == 200 and h.wfile.getvalue() == model.read_bytes()
        assert h.response_headers["Content-Type"] == "application/gzip"
        model.unlink()
        h = Handler("/ulasim-api/offline-model")
        proxy.proxy_transport(h)
        assert h.status == 503
        h = Handler("/ulasim-api/offline-model?x=1")
        proxy.proxy_transport(h)
        assert h.status == 404
    assert proxy.STATIC["/offline-model"][0] == proxy.ROOT / "data/offline-model.json.gz"


# ------------------------------------------------------------------ statik yenileme
def test_static_refresh_builds_files_and_keeps_old_on_failure(tmp_path):
    old = {"hatlar.json": {"161": {"kod": "H/1", "yon": 2, "ad": "eski"}, "777": {"kod": "X/7", "yon": 1, "ad": "kalkan"}},
           "duraklar.json": {"161": [{"id": 1}], "777": [{"id": 9}]},
           "seferler.json": {"H/1": [{"lineSchedules": {}}]}, "sekiller.json": {"161": [[1, 2]]}}
    stop = {"code": "1", "id": 4852, "location": {"latitude": 37.6, "longitude": 36.8}, "name": "A", "sequenceNo": 1}

    def fn(op, p):
        if op == "Search":
            return {"busLines": [{"id": 161, "direction": 2, "code": "H/1", "name": "H/1 (Dönüş)"},
                                 {"id": 160, "direction": 1, "code": "H/1", "name": "H/1 (Gidiş)"}]}
        if op == "LineStop":
            if p["lineId"] == 777:
                return {"lineStops": []}
            if p["lineId"] == 160:
                return ProviderError("UPSTREAM_TIMEOUT", "x", 504)
            return {"lineStops": [stop, {**stop, "id": 4853, "sequenceNo": 2}]}
        if op == "BusLinePoints":
            return {"lineCoordinates": [{"latitude": 37.6, "longitude": 36.8}]}
        if op == "TimeTableAllDirection":
            return {"timeTable": [{"lineSchedules": {"WORKDAY": ["06:00"]}, "routeType": 2}]}

    provider = FakeProvider(fn)
    pauses = []

    async def sleep(s):
        pauses.append(s)

    r = sr.Refresher(provider, pause=1.0, sleep=sleep, log=lambda *_: None)
    new = asyncio.run(r.build(old))
    assert all(ttl == 0 for *_, ttl in provider.calls) and set(pauses) == {1.0}
    assert provider.max_inflight == 1
    assert set(new["hatlar.json"]) == {"161"}  # 777 durak döndürmüyor, 160 alınamadı ve eskide yok
    assert new["hatlar.json"]["161"] == {"kod": "H/1", "yon": 2, "ad": "H/1 (Dönüş)"}
    assert [s["id"] for s in new["duraklar.json"]["161"]] == [4852, 4853]
    assert new["sekiller.json"]["161"] == [[36.8, 37.6]]
    assert new["seferler.json"]["H/1"][0]["routeType"] == 2
    # %80 altına düşen dosya yazılmaz
    sdir = tmp_path / "s"
    written = sr.publish(new, old, sdir, log=lambda *_: None)
    assert "hatlar.json" not in written and "duraklar.json" not in written
    assert set(written) == {"sekiller.json", "seferler.json"}


def test_static_refresh_aborts_on_auth_failure():
    provider = FakeProvider(lambda op, p: ProviderError("UPSTREAM_AUTH_FAILED", "x"))

    async def sleep(s):
        pass

    r = sr.Refresher(provider, sleep=sleep, log=lambda *_: None)
    with pytest.raises(sr.RefreshAborted):
        asyncio.run(r.build({"hatlar.json": {"1": {"kod": "A", "yon": 1}}, "duraklar.json": {},
                             "seferler.json": {}, "sekiller.json": {}}))
    assert len(provider.calls) == 1


# ------------------------------------------------------------------ model yenileme
HANDOFF = Path("/home/alya/maras-devir/veri-araclari")


def test_rebuild_decision(tmp_path):
    raw = tmp_path / "anlik"
    raw.mkdir()
    now = datetime(2026, 11, 13, 0, 30, tzinfo=timezone.utc)
    assert model_yenile.decide(tmp_path / "m.json", raw, now, 48)[0] is False  # veri yok
    (raw / "2026-11-12.jsonl.gz").write_bytes(b"")
    os.utime(raw / "2026-11-12.jsonl.gz", (now.timestamp() - 3600,) * 2)
    assert model_yenile.decide(tmp_path / "m.json", raw, now, 48) == (True, "model yok")
    meta = tmp_path / "m.json"
    meta.write_text(json.dumps({"built_at": "2026-11-12T00:30:00Z"}))
    assert model_yenile.decide(meta, raw, now, 48)[0] is False  # 24 saatlik
    meta.write_text(json.dumps({"built_at": "2026-11-10T00:30:00Z"}))
    assert model_yenile.decide(meta, raw, now, 48)[0] is True
    os.utime(raw / "2026-11-12.jsonl.gz", (datetime(2026, 11, 9, tzinfo=timezone.utc).timestamp(),) * 2)
    assert model_yenile.decide(meta, raw, now, 48)[0] is False  # yeni veri yok


def test_validate_rejects_mismatch(tmp_path):
    model = {"schema": 1, "model_version": "v1", "built_at": "2026-11-13T00:00:00Z", "lines": [{}], "stops": [{}]}
    gz = gzip.compress(json.dumps(model).encode(), mtime=0)
    (tmp_path / "offline-model.json.gz").write_bytes(gz)
    meta = {"schema": 1, "model_version": "v1", "built_at": "2026-11-13T00:00:00Z",
            "sha256": hashlib.sha256(gz).hexdigest(), "size_bytes": len(gz)}
    (tmp_path / "model-meta.json").write_text(json.dumps(meta))
    assert model_yenile.validate(tmp_path / "offline-model.json.gz", tmp_path / "model-meta.json")["model_version"] == "v1"
    (tmp_path / "model-meta.json").write_text(json.dumps({**meta, "model_version": "v2"}))
    with pytest.raises(ValueError):
        model_yenile.validate(tmp_path / "offline-model.json.gz", tmp_path / "model-meta.json")


@pytest.mark.skipif(not HANDOFF.is_dir(), reason="devir paketi yok")
def test_rebuild_end_to_end_with_collector_output_and_quality_gate(tmp_path):
    import shutil
    data = tmp_path / "data"
    shutil.copytree(HANDOFF / "statik", data / "statik")
    shutil.copytree(HANDOFF / "veri", data / "anlik")
    # Toplayıcının yazdığı biçimde (ayrı gzip üyesi) ek bir kayıt: üretici okuyabilmeli
    col.append_gzip(data / "anlik" / "2026-11-12.jsonl.gz", [json.dumps(col.build_record(
        "2026-11-12T23:50:00+03:00", "2026-11-12T20:50:01+00:00", 161, "H/1", 2,
        {"busLineInfo": {"liveBuses": [BUS]}}))])
    script = ROOT / "tools" / "model_yenile.py"
    r = subprocess.run([sys.executable, script, "--veri-dizini", data], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    meta = model_yenile.validate(data / "offline-model.json.gz", data / "model-meta.json")
    assert meta["lines"]["with_estimates"] >= 80 and meta["schema"] == 1
    assert not list(data.glob(".model-*"))
    # 48 saat dolmadan yeniden üretmez
    r = subprocess.run([sys.executable, script, "--veri-dizini", data], capture_output=True, text=True)
    assert r.returncode == 0 and "atla" in r.stdout
    # Kalite kapısı: veri azalırsa eski model yerinde kalır
    before = (data / "model-meta.json").read_bytes()
    for p in sorted((data / "ozet").glob("*.json"))[:-1]:
        p.unlink()
    for p in sorted((data / "anlik").glob("*.jsonl.gz"))[:-1]:
        p.unlink()
    r = subprocess.run([sys.executable, script, "--veri-dizini", data, "--zorla"], capture_output=True, text=True)
    assert r.returncode == 2, r.stdout + r.stderr
    assert (data / "model-meta.json").read_bytes() == before
    model_yenile.validate(data / "offline-model.json.gz", data / "model-meta.json")
