#!/usr/bin/env python3
"""Gömülü durak listesine her durağa uğrayan hat kodlarını ekler ve hiçbir hattın uğramadığı
durakları çıkarır. Sorgusu başarısız olan durak listede kalır (hat bilgisi olmadan).

Kullanım: python3 tools/durak_hatlari.py [api_adresi]
"""
import concurrent.futures as cf
import json
import pathlib
import sys
import time
import urllib.request

API = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
DOSYA = pathlib.Path(__file__).resolve().parents[2] / "maras-ulasim/app/src/main/assets/all-stops.json"


def hatlar(durak_id):
    for deneme in range(4):
        try:
            with urllib.request.urlopen(f"{API}/v1/stops/{durak_id}/lines", timeout=20) as r:
                veri = json.load(r)["data"]
            return sorted({h["lineCode"] for h in veri.get("stopLines") or [] if h.get("lineCode")},
                          key=lambda k: (len(k), k))
        except Exception:
            time.sleep(1.5 * (deneme + 1))
    return None


def main():
    veri = json.loads(DOSYA.read_text())
    duraklar = veri["stops"]
    with cf.ThreadPoolExecutor(3) as havuz:
        sonuclar = list(havuz.map(lambda d: hatlar(d["id"]), duraklar))
    kalan, bos, hata = [], 0, 0
    for durak, kodlar in zip(duraklar, sonuclar):
        if kodlar is None:
            hata += 1
            kalan.append(durak)
        elif kodlar:
            kalan.append({**durak, "lines": kodlar})
        else:
            bos += 1
    veri["stops"] = kalan
    veri["lines_checked"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    DOSYA.write_text(json.dumps(veri, ensure_ascii=False, separators=(",", ":")))
    print(f"{len(kalan)} durak kaldı, hatsız {bos} durak çıkarıldı, {hata} durak sorgulanamadı", file=sys.stderr)


if __name__ == "__main__":
    main()
