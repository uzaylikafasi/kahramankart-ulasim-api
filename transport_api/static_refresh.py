"""Haftalık statik veri yenileme: hat listesi, hat durakları, tarifeler, güzergâh şekilleri.

Çıktı (model_uret.py girdisi, docs/offline-model-sema.md sonu):
  hatlar.json   {hatId: {kod, yon, ad}}          Search (hat koduyla, yalnız hatlar)
  duraklar.json {hatId: lineStops[]}             LineStop
  seferler.json {kod: timeTable[]}               TimeTableAllDirection
  sekiller.json {hatId: [[boylam, enlem], ...]}  BusLinePoints

Sağlayıcıyı doğrudan, tek tek ve aralarında bekleyerek çağırır. Hat keşfi mevcut hatlar.json'daki
kodlardan yapılır (Search en çok 10 sonuç döner), bu yüzden ilk hatlar.json elde olmalıdır.
Bir dosya ancak ayrıştırılıp makul görünürse (boş değil, önceki kayıt sayısının en az %80'i)
atomik olarak değiştirilir; tek bir hattın sorgusu başarısızsa o hattın eski kaydı korunur.

Kullanım: .venv/bin/python -m transport_api.static_refresh [--statik DIZIN] [--bekleme 1.0] [--cikti DIZIN]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from .collector import data_dir
from .provider import ProviderError

FILES = ("hatlar.json", "duraklar.json", "seferler.json", "sekiller.json")
MIN_RATIO = 0.8
MAX_CONSECUTIVE_FAILURES = 5


class RefreshAborted(Exception):
    pass


def read_static(sdir: Path) -> dict[str, dict]:
    out = {}
    for name in FILES:
        try:
            value = json.loads((sdir / name).read_text(encoding="utf-8"))
            out[name] = value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            out[name] = {}
    return out


def parse_search_lines(data: dict) -> dict[str, dict]:
    found = {}
    for item in data.get("busLines") or []:
        if not isinstance(item, dict):
            continue
        try:
            hid, direction, code = int(item["id"]), int(item["direction"]), str(item["code"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if hid > 0 and direction in (1, 2, 3) and code:
            found[str(hid)] = {"kod": code, "yon": direction, "ad": item.get("name") or ""}
    return found


def parse_line_stops(data: dict) -> list | None:
    stops = data.get("lineStops")
    if not isinstance(stops, list):
        return None
    good = [s for s in stops if isinstance(s, dict) and isinstance(s.get("id"), int)
            and isinstance(s.get("location"), dict)]
    return good if len(good) == len(stops) else None


def parse_shape(data: dict) -> list | None:
    coords = data.get("lineCoordinates")
    if not isinstance(coords, list):
        return None
    try:
        pts = [[float(c["longitude"]), float(c["latitude"])] for c in coords]
    except (KeyError, TypeError, ValueError):
        return None
    if any(not (-180 <= lon <= 180 and -90 <= lat <= 90) for lon, lat in pts):
        return None
    return pts


def parse_timetable(data: dict) -> list | None:
    table = data.get("timeTable")
    if not isinstance(table, list) or not all(isinstance(e, dict) and isinstance(e.get("lineSchedules"), dict) for e in table):
        return None
    return table


def sane(new: dict, old: dict) -> bool:
    return bool(new) and len(new) >= MIN_RATIO * len(old)


def write_atomic(path: Path, value: Any) -> None:
    tmp = path.with_name(".tmp-" + path.name)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Refresher:
    def __init__(self, provider, pause: float = 1.0, sleep=asyncio.sleep, log=print):
        self.provider, self.pause, self.sleep, self.log = provider, pause, sleep, log
        self.consecutive = 0
        self.failures = 0
        self.calls = 0

    async def call(self, operation: str, params: dict):
        """Tek çağrı; hata None döner. Yetki hatası ya da art arda çok hata tüm yenilemeyi durdurur."""
        if self.calls:
            await self.sleep(self.pause)
        self.calls += 1
        try:
            result = await self.provider.get(operation, params, ttl=0)
        except ProviderError as exc:
            self.failures += 1
            self.consecutive += 1
            if exc.code == "UPSTREAM_AUTH_FAILED":
                raise RefreshAborted("Pallium erişim bilgisi reddedildi (UPSTREAM_AUTH_FAILED)") from None
            if self.consecutive >= MAX_CONSECUTIVE_FAILURES:
                raise RefreshAborted(f"{MAX_CONSECUTIVE_FAILURES} art arda hata, son: {exc.code}") from None
            return None
        self.consecutive = 0
        return result.data

    async def build(self, old: dict[str, dict]) -> dict[str, dict]:
        old_lines = old["hatlar.json"]
        codes = sorted({str(h.get("kod")) for h in old_lines.values() if isinstance(h, dict) and h.get("kod")})
        if not codes:
            raise RefreshAborted("hatlar.json yok ya da boş; hat keşfi için önceki liste gerekli")
        found: dict[str, dict] = {}
        for code in codes:
            data = await self.call("Search", {"SearchString": code, "busStops": 0, "adress": 0, "busLines": 1})
            if data is not None:
                found.update(parse_search_lines(data))
        lines = {**{k: v for k, v in old_lines.items() if isinstance(v, dict)}, **found}
        stops, shapes = {}, {}
        for hid in sorted(lines, key=lambda h: int(h) if h.isdigit() else 0):
            h = lines[hid]
            data = await self.call("LineStop", {"lineId": int(hid), "direction": int(h["yon"])})
            parsed = parse_line_stops(data) if data is not None else None
            if parsed is None:
                if hid in old["duraklar.json"]:
                    stops[hid] = old["duraklar.json"][hid]  # geçici hata: eski kayıt
                    self.log(f"uyarı: {hid} durakları alınamadı, eskisi korunuyor")
            elif parsed:
                stops[hid] = parsed
            if hid not in stops:
                if hid not in found:
                    self.log(f"bilgi: {hid} ({h.get('kod')}) artık durak döndürmüyor, listeden çıkarıldı")
                continue
            data = await self.call("BusLinePoints", {"lineId": int(hid), "direction": int(h["yon"])})
            parsed = parse_shape(data) if data is not None else None
            if parsed:
                shapes[hid] = parsed
            elif hid in old["sekiller.json"]:
                shapes[hid] = old["sekiller.json"][hid]
        lines = {k: v for k, v in lines.items() if k in stops}
        tables = {}
        for code in sorted({h["kod"] for h in lines.values()}):
            data = await self.call("TimeTableAllDirection", {"lineCode": code})
            parsed = parse_timetable(data) if data is not None else None
            if parsed is not None:
                tables[code] = parsed
            elif code in old["seferler.json"]:
                tables[code] = old["seferler.json"][code]
        return {"hatlar.json": lines, "duraklar.json": stops, "seferler.json": tables, "sekiller.json": shapes}


def publish(new: dict[str, dict], old: dict[str, dict], out: Path, log=print, skip_unchanged=True) -> list[str]:
    """Makul dosyaları yaz. hatlar.json en son yazılır ve yalnız durakları da yazılabildiyse."""
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name in ("duraklar.json", "sekiller.json", "seferler.json", "hatlar.json"):
        if not sane(new[name], old[name]):
            log(f"atlandı: {name} makul değil ({len(new[name])} kayıt, önceki {len(old[name])})")
            continue
        if name == "hatlar.json" and "duraklar.json" not in written and new[name].keys() - old["duraklar.json"].keys():
            log("atlandı: hatlar.json (yeni hatların durakları yazılamadı)")
            continue
        if skip_unchanged and new[name] == old[name]:
            continue
        write_atomic(out / name, new[name])
        written.append(name)
    return written


async def main_async(args) -> int:
    from .provider import create_provider

    sdir = Path(args.statik)
    old = read_static(sdir)
    provider = create_provider()
    try:
        refresher = Refresher(provider, pause=args.bekleme)
        try:
            new = await refresher.build(old)
        except RefreshAborted as exc:
            print(f"DURDU: {exc}; hiçbir dosya değiştirilmedi", file=sys.stderr)
            return 2
    finally:
        await provider.close()
    written = publish(new, old, Path(args.cikti or sdir), skip_unchanged=not args.cikti)
    print(json.dumps({"calls": refresher.calls, "failures": refresher.failures, "written": written,
                      "counts": {k: len(v) for k, v in new.items()}}, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    from dotenv import load_dotenv
    from .provider import ROOT

    load_dotenv(ROOT / ".env", override=False)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--statik", default=str(data_dir() / "statik"))
    ap.add_argument("--cikti", help="farklı bir dizine yaz (deneme için); varsayılan --statik")
    ap.add_argument("--bekleme", type=float, default=1.0, help="çağrılar arası saniye")
    return asyncio.run(main_async(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
