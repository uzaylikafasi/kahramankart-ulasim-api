#!/usr/bin/env python3
"""Kahramankart ulaşım verileri için salt okunur komut satırı istemcisi."""
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://maras.pallium.com.tr/PalliumMarasServices/Service.svc"
AUTH = Path(__file__).parent / "analysis" / "transport-auth.json"


def fetch(endpoint, params):
    if value := os.environ.get("PALLIUM_AUTHORIZATION"):
        headers = {"Authorization": value}
    else:
        auth_path = Path(os.environ.get("PALLIUM_AUTH_FILE", str(AUTH)))
        if not auth_path.exists() and "PALLIUM_AUTH_FILE" not in os.environ:
            auth_path = Path.home() / ".config/kahramankart-ulasim-api/transport-auth.json"
        headers = json.loads(auth_path.read_text())
    url = BASE + "/" + endpoint
    if params:
        url += "?" + urlencode(params)
    with urlopen(Request(url, headers=headers), timeout=25) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    search = sub.add_parser("ara", help="Hat ve durak ara")
    search.add_argument("metin")
    for command in ("durak", "durak-hatlari"):
        p = sub.add_parser(command)
        p.add_argument("durak_id", type=int)
    for command in ("hat", "hat-duraklari", "guzergah"):
        p = sub.add_parser(command)
        p.add_argument("hat_id", type=int)
        p.add_argument("--yon", type=int, choices=(1, 2, 3), default=1)
    times = sub.add_parser("saatler")
    times.add_argument("hat_kodu", help="Örnek: H/1")
    sub.add_parser("duyurular")
    args = parser.parse_args()
    if args.command == "ara":
        endpoint, params = "Search", dict(SearchString=args.metin, busStops=1, adress=0, busLines=1)
    elif args.command in ("durak", "durak-hatlari"):
        endpoint = "BusStop" if args.command == "durak" else "StopLines"
        params = {"stopId": args.durak_id}
    elif args.command in ("hat", "hat-duraklari", "guzergah"):
        endpoint = {"hat": "BusLine", "hat-duraklari": "LineStop", "guzergah": "BusLinePoints"}[args.command]
        params = {"lineId": args.hat_id, "direction": args.yon}
    elif args.command == "saatler":
        endpoint, params = "TimeTableAllDirection", {"lineCode": args.hat_kodu}
    else:
        endpoint, params = "Announcements", {}
    try:
        data = fetch(endpoint, params)
    except FileNotFoundError:
        parser.exit(1, f"Yerel erişim dosyası bulunamadı: {AUTH}\n")
    except HTTPError as exc:
        parser.exit(1, f"API HTTP {exc.code} döndürdü. 401 durumunda yerel erişim bilgisi yenilenmelidir.\n")
    except (URLError, TimeoutError, ValueError) as exc:
        parser.exit(1, f"API isteği tamamlanamadı: {exc}\n")
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
