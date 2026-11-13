#!/usr/bin/env python3
"""Çevrimdışı modeli yeniden üret ve yayına al (systemd zamanlayıcısı gece çalıştırır).

Akış: model 48 saatten eskiyse ve son modelden sonra yeni ham veri geldiyse
  1) model_uret.py ozet  (yalnız değişen günleri özetler)
  2) model_uret.py model (son 28 gün özeti, --onceki-meta ile kalite kapısı) -> geçici dizin
  3) doğrulama: sha256, boyut, şema, model_version ve built_at meta ile dosyada aynı
  4) os.replace ile önce .gz, sonra meta (meta hiç olmayan dosyayı göstermez)
Herhangi bir hatada eski model yerinde kalır, çıkış kodu sıfırdan farklıdır.

Yalnız standart kütüphane. Kullanım: python3 tools/model_yenile.py [--veri-dizini data] [--zorla]
"""
import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = Path(__file__).resolve().parent / "model_uret.py"
MODEL = "offline-model.json.gz"
META = "model-meta.json"
MAX_BYTES = 30_000_000


def parse_utc(value):
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def validate(gz_path, meta_path):
    """Uygulamanın yaptığı kontrollerin aynısı. Hata varsa ValueError."""
    meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    raw = Path(gz_path).read_bytes()
    if len(raw) > MAX_BYTES:
        raise ValueError(f"model {len(raw)} bayt, sınır {MAX_BYTES}")
    if hashlib.sha256(raw).hexdigest() != meta.get("sha256"):
        raise ValueError("sha256 uyuşmuyor")
    if meta.get("size_bytes") != len(raw):
        raise ValueError("size_bytes uyuşmuyor")
    model = json.loads(gzip.decompress(raw).decode("utf-8"))
    for key in ("schema", "model_version", "built_at"):
        if model.get(key) != meta.get(key) or not meta.get(key):
            raise ValueError(f"{key} uyuşmuyor")
    if meta["schema"] != 1:
        raise ValueError("şema 1 değil")
    parse_utc(meta["built_at"])
    if not model.get("lines") or not model.get("stops"):
        raise ValueError("model boş")
    return meta


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def install(build_dir, serve_dir):
    for name in (MODEL, META):
        os.chmod(Path(build_dir) / name, 0o644)  # mkstemp 0600 açar; yalnız herkese açık veri
    os.replace(Path(build_dir) / MODEL, Path(serve_dir) / MODEL)
    fsync_dir(serve_dir)
    os.replace(Path(build_dir) / META, Path(serve_dir) / META)
    fsync_dir(serve_dir)


def prune_summaries(ozet_dir, today, keep_days):
    limit = today - dt.timedelta(days=keep_days)
    for p in Path(ozet_dir).glob("*.json"):
        try:
            if dt.date.fromisoformat(p.stem) < limit:
                p.unlink()
        except ValueError:
            continue


def decide(meta_path, raw_dir, now, min_age_hours):
    """(üretilsin mi, neden)."""
    raws = sorted(Path(raw_dir).glob("*.jsonl.gz")) + sorted(Path(raw_dir).glob("*.jsonl"))
    if not raws:
        return False, "ham veri yok"
    if not Path(meta_path).exists():
        return True, "model yok"
    try:
        built = parse_utc(json.loads(Path(meta_path).read_text(encoding="utf-8"))["built_at"])
    except (OSError, ValueError, KeyError):
        return True, "meta okunamadı"
    age = (now - built).total_seconds() / 3600
    if age < min_age_hours:
        return False, f"model {age:.1f} saatlik (< {min_age_hours})"
    newest = max(p.stat().st_mtime for p in raws)
    if newest <= built.timestamp():
        return False, "son modelden sonra yeni ham veri yok"
    return True, f"model {age:.1f} saatlik, yeni veri var"


def run(cmd):
    print("+", " ".join(map(str, cmd)), flush=True)
    return subprocess.run(list(map(str, cmd)), check=False).returncode


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--veri-dizini", default=os.getenv("MARAS_DATA_DIR", str(ROOT / "data")))
    ap.add_argument("--en-az-saat", type=float, default=48)
    ap.add_argument("--gun-sayisi", type=int, default=28)
    ap.add_argument("--saklama-gun", type=int, default=60)
    ap.add_argument("--zorla", action="store_true", help="yaş ve yeni veri kontrolünü atla (kalite kapısı yine çalışır)")
    a = ap.parse_args(argv)
    data = Path(a.veri_dizini)
    raw, static, ozet = data / "anlik", data / "statik", data / "ozet"
    meta_path = data / META
    now = dt.datetime.now(dt.timezone.utc)
    ok, why = decide(meta_path, raw, now, a.en_az_saat)
    print(f"karar: {'üret' if ok or a.zorla else 'atla'} ({why}{', --zorla' if a.zorla else ''})", flush=True)
    if not ok and not a.zorla:
        return 0
    if not raw.is_dir() or not any(raw.iterdir()):
        print("ham veri yok", file=sys.stderr)
        return 0
    py = sys.executable
    if run([py, BUILDER, "ozet", "--veri", raw, "--statik", static, "--ozet", ozet]) != 0:
        print("HATA: özet adımı başarısız, eski model korunuyor", file=sys.stderr)
        return 1
    prune_summaries(ozet, now.date(), a.saklama_gun)
    build = Path(tempfile.mkdtemp(prefix=".model-", dir=data))
    try:
        cmd = [py, BUILDER, "model", "--ozet", ozet, "--statik", static, "--cikti", build, "--gun-sayisi", a.gun_sayisi]
        if meta_path.exists():
            cmd += ["--onceki-meta", meta_path]
        if run(cmd) != 0:
            print("HATA: model adımı başarısız ya da kalite kapısı reddetti, eski model korunuyor", file=sys.stderr)
            return 2
        try:
            meta = validate(build / MODEL, build / META)
        except (OSError, ValueError) as exc:
            print(f"HATA: yeni model doğrulanamadı ({exc}), eski model korunuyor", file=sys.stderr)
            return 3
        install(build, data)
        print(json.dumps({k: meta.get(k) for k in ("model_version", "built_at", "size_bytes", "days", "pairs", "lines")},
                         ensure_ascii=False), flush=True)
        return 0
    finally:
        shutil.rmtree(build, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
