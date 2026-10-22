#!/usr/bin/env python3
"""Şehirdeki bütün durakları toplar. Kaynak bir bölge için en fazla 20 durak döndürdüğünden
bölge, 20'den az sonuç verene kadar dörde bölünür. Çıktı: Android'e gömülen durak listesi.

Kullanım: python3 tools/durak_listesi.py [api_adresi] > ../maras-ulasim/app/src/main/assets/all-stops.json
"""
import concurrent.futures as cf
import json
import sys
import time
import urllib.request

API = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
SINIR = 20
ALAN = (36.90, 36.20, 38.70, 38.00)  # güney, batı, kuzey, doğu: Kahramanmaraş ili ve çevresi


def sorgu(g, b, k, d):
    url = f"{API}/v1/stops/in-bounds?south={g:.6f}&west={b:.6f}&north={k:.6f}&east={d:.6f}"
    for deneme in range(4):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.load(r)["data"].get("busStops") or []
        except Exception:
            time.sleep(1.5 * (deneme + 1))
    raise RuntimeError(f"Bölge alınamadı: {url}")


def main():
    duraklar, bekleyen, istek = {}, [ALAN], 0
    with cf.ThreadPoolExecutor(6) as havuz:
        while bekleyen:
            parca, bekleyen = bekleyen, []
            for kutu, sonuc in zip(parca, havuz.map(lambda k: sorgu(*k), parca)):
                istek += 1
                for s in sonuc:
                    duraklar[str(s["busStopId"])] = s
                g, b, k, d = kutu
                if len(sonuc) >= SINIR and (k - g) > 0.0005:
                    og, ob = (g + k) / 2, (b + d) / 2
                    bekleyen += [(g, b, og, ob), (g, ob, og, d), (og, b, k, ob), (og, ob, k, d)]
    liste = sorted(({"id": int(s["busStopId"]), "code": s.get("busStopCode", ""), "name": s.get("name", ""),
                     "lat": round(s["location"]["latitude"], 6), "lng": round(s["location"]["longitude"], 6)}
                    for s in duraklar.values() if s.get("location")), key=lambda x: x["id"])
    print(f"{len(liste)} durak, {istek} istek", file=sys.stderr)
    json.dump({"generated": time.strftime("%Y-%m-%d"), "stops": liste}, sys.stdout, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main()
