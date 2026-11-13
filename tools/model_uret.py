#!/usr/bin/env python3
"""Maraş Ulaşım çevrimdışı model üretici (şema v1: docs/offline-model-sema.md).

Girdi : toplayıcının ham kayıtları (*.jsonl / *.jsonl.gz, satır başına bir hat yönü, tur sırasıyla)
        + statik veri (hatlar.json, duraklar.json, seferler.json, sekiller.json).
Çıktı : offline-model.json.gz + model-meta.json (atomik yazım).

Yöntem: resmi tarife kalkışları + öğrenilen kümülatif durak payları. Aynı plakanın ardışık iki
gözleminden (konum, süre) çiftleri çıkar; hat profili (durak payları) ridge en küçük kareler ile,
sefer süresi (gün tipi x saat) oran tahmincisi ile bulunur. Örnek azsa hücre null (uygulama tarifeye düşer).

Alt komutlar:
  uret    --veri D --statik D --cikti D [--ozet D]   ham -> özet -> model (varsayılan)
  ozet    --veri D --statik D --ozet D               yalnız gün özetleri
  model   --ozet D --statik D --cikti D              özetlerden model (sunucu 48 saatlik yeniden üretim)
  dogrula --veri D --statik D                        bir gün dışarıda bırakma doğrulaması + rota tipi kanıtı

Yalnız standart kütüphane; yollar bağımsız değişken; dosyalar akış olarak okunur.
"""
import argparse
import datetime as dt
import glob
import gzip
import hashlib
import json
import math
import os
import statistics
import sys
import tempfile
from collections import defaultdict

SCHEMA = 1
GENERATOR = "model_uret.py 1.0"
DAY_TYPES = ("WORKDAY", "SATURDAY", "SUNDAY")
TZ = dt.timezone(dt.timedelta(hours=3))  # Europe/Istanbul (yıl boyu +03)
MAX_GAP_S = 25 * 60       # ardışık iki gözlem arası en çok
MIN_ADVANCE = 0.3         # çift için en az ilerleme (durak birimi)
END_MARGIN = 1.0          # uçlardaki bekleme etkisini dışla
MIN_PAIRS = 6
MIN_DAYS = 2
MIN_SPAN = 1.0
PROFILE_MIN_PAIRS = 15
RIDGE = 1.0


def day_type(d):
    w = d.weekday()
    return "WORKDAY" if w < 5 else ("SATURDAY" if w == 5 else "SUNDAY")


# ------------------------------------------------------------------ statik
def read_json(path):
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def haversine(a, b):
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def load_static(sdir):
    hatlar = read_json(os.path.join(sdir, "hatlar.json"))
    duraklar = read_json(os.path.join(sdir, "duraklar.json"))
    seferler = read_json(os.path.join(sdir, "seferler.json"))
    sp = os.path.join(sdir, "sekiller.json")
    sekiller = read_json(sp) if os.path.exists(sp) else {}
    lines = {}
    stops = {}
    for hid, h in hatlar.items():
        raw = duraklar.get(hid)
        if not raw:
            continue
        seq = sorted(raw, key=lambda s: s.get("sequenceNo", 0))
        ids = [int(s["id"]) for s in seq]
        for s in seq:
            loc = s.get("location") or {}
            stops[int(s["id"])] = {"id": int(s["id"]), "code": str(s.get("code") or ""), "name": s.get("name") or "",
                                   "lat": loc.get("latitude"), "lon": loc.get("longitude")}
        coords = [(stops[i]["lat"], stops[i]["lon"]) for i in ids]
        seg = {}
        for k in range(len(ids) - 1):
            seg.setdefault((ids[k], ids[k + 1]), k)
        name = h.get("ad") or ""
        dn = name[name.rfind("(") + 1:name.rfind(")")] if "(" in name and name.endswith(")") else ("Gidiş" if h["yon"] == 1 else "Dönüş")
        lines[hid] = {"id": int(hid), "dir": int(h["yon"]), "code": h["kod"], "name": name, "dirName": dn,
                      "stops": ids, "coords": coords, "seg": seg,
                      "hash": hashlib.sha1(",".join(map(str, ids)).encode()).hexdigest()[:8],
                      "shape": sekiller.get(hid)}
    return lines, stops, seferler


def route_type_for(line, entries):
    """Kanıt (docs/offline-model-sema.md, `dogrula` çıktısı): routeType == yön. Tek kayıt varsa onu al, işaretle."""
    by = {e.get("routeType"): e for e in entries}
    if line["dir"] in by:
        return line["dir"], "direction"
    if len(entries) == 1:
        return entries[0].get("routeType"), "single"
    return None, "none"


# ------------------------------------------------------------------ gözlem akışı
def open_text(path):
    return gzip.open(path, "rt", encoding="utf-8") if path.endswith(".gz") else open(path, "r", encoding="utf-8")


def parse_time(s):
    return dt.datetime.fromisoformat(s).astimezone(TZ)


def position(line, o, s, lat, lon):
    """Araç konumu durak birimi cinsinden (i + kesir) ya da None."""
    try:
        k = line["seg"][(int(o), int(s))]
    except (KeyError, TypeError, ValueError):
        return None
    a, b = line["coords"][k], line["coords"][k + 1]
    c = 0.79
    ex, ey = (b[1] - a[1]) * c, b[0] - a[0]
    L = ex * ex + ey * ey
    if L <= 0:
        return float(k)
    f = ((lon - a[1]) * c * ex + (lat - a[0]) * ey) / L
    return k + max(0.0, min(1.0, f))


class DaySummary:
    def __init__(self, label):
        self.label = label
        self.rounds = set()
        self.round_hours = defaultdict(lambda: defaultdict(set))  # tarih -> saat -> tur kümesi
        self.samples = 0
        self.first = None
        self.last = None
        self.pairs = defaultdict(list)  # hid -> [(u1,u2,dt_s,mid_dakika,tarih_ord,gün_tipi_no)]
        self.last_obs = {}

    def add(self, rec, lines):
        line = lines.get(str(rec["h"]))
        t = parse_time(rec.get("t") or rec["tur"])
        self.rounds.add(rec["tur"])
        self.round_hours[t.date().isoformat()][t.hour].add(rec["tur"])
        if self.first is None or t < self.first:
            self.first = t
        if self.last is None or t > self.last:
            self.last = t
        if line is None:
            return
        n = len(line["stops"])
        for v in rec.get("v") or []:
            self.samples += 1
            u = position(line, v.get("o"), v.get("s"), v.get("la"), v.get("lo"))
            plate = v.get("p")
            if u is None or not plate:
                continue
            key = (rec["h"], plate)
            prev = self.last_obs.get(key)
            self.last_obs[key] = (t, u)
            if prev is None:
                continue
            gap = (t - prev[0]).total_seconds()
            if not (30 <= gap <= MAX_GAP_S):
                continue
            if u - prev[1] < MIN_ADVANCE or prev[1] < END_MARGIN or u > (n - 1) - END_MARGIN:
                continue
            mid = prev[0] + (t - prev[0]) / 2
            self.pairs[str(rec["h"])].append((round(prev[1], 3), round(u, 3), int(gap),
                                              mid.hour * 60 + mid.minute, mid.date().toordinal()))

    def to_json(self, lines):
        return {"v": 1, "label": self.label, "samples": self.samples, "rounds": len(self.rounds),
                "first": self.first.isoformat() if self.first else None,
                "last": self.last.isoformat() if self.last else None,
                "round_hours": {d: {str(h): len(r) for h, r in hs.items()} for d, hs in self.round_hours.items()},
                "lines": {hid: {"h": lines[hid]["hash"], "pairs": p} for hid, p in self.pairs.items() if hid in lines}}


def summarize_file(path, lines):
    ds = DaySummary(os.path.basename(path).split(".")[0])
    with open_text(path) as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue  # yarım satır (yazma sırasında kesilmiş)
            ds.add(rec, lines)
    return ds.to_json(lines)


def data_files(vdir):
    return sorted(glob.glob(os.path.join(vdir, "*.jsonl")) + glob.glob(os.path.join(vdir, "*.jsonl.gz")))


# ------------------------------------------------------------------ tahmin
def solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            continue
        M[c], M[p] = M[p], M[c]
        for r in range(c + 1, n):
            f = M[r][c] / M[c][c]
            if f:
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = M[r][n] - sum(M[r][k] * x[k] for k in range(r + 1, n))
        x[r] = s / M[r][r] if abs(M[r][r]) > 1e-12 else 0.0
    return x


def overlaps(u1, u2, m):
    out = []
    for k in range(int(u1), min(int(math.ceil(u2)), m)):
        w = min(u2, k + 1) - max(u1, k)
        if w > 0:
            out.append((k, w))
    return out


def cell_key(p):
    mid, ordn = p[3], p[4]
    d = dt.date.fromordinal(ordn)
    return day_type(d), mid // 60


def estimate_line(line, pairs):
    """pairs: [(u1,u2,dt,mid,ordinal)] -> (cum, profile_n, basis, trip_cells, T_all)."""
    n = len(line["stops"])
    m = n - 1
    L = [max(20.0, haversine(line["coords"][k], line["coords"][k + 1])) for k in range(m)]
    prior = [x / sum(L) for x in L]
    tau = prior[:]
    ov = [overlaps(p[0], p[1], m) for p in pairs]
    keys = [cell_key(p) for p in pairs]
    basis = "geometry"
    if len(pairs) >= PROFILE_MIN_PAIRS:
        basis = "learned"
        for _ in range(4):
            span = [sum(w * tau[k] for k, w in o) for o in ov]
            sums = defaultdict(lambda: [0.0, 0.0])
            for p, s, key in zip(pairs, span, keys):
                sums[key][0] += p[2]
                sums[key][1] += s
            tot = sum(p[2] for p in pairs) / max(1e-9, sum(span))
            def T(key):
                a = sums[key]
                return a[0] / a[1] if a[1] > 0.3 else tot
            y = [p[2] / T(key) for p, key in zip(pairs, keys)]
            A = [[0.0] * m for _ in range(m)]
            b = [0.0] * m
            for o, yj in zip(ov, y):
                for k1, w1 in o:
                    b[k1] += w1 * yj
                    for k2, w2 in o:
                        A[k1][k2] += w1 * w2
            for k in range(m):
                A[k][k] += RIDGE
                b[k] += RIDGE * prior[k]
            x = solve(A, b)
            x = [max(0.15 * prior[k], x[k]) for k in range(m)]
            s = sum(x)
            tau = [v / s for v in x]
    cum = [0.0]
    for v in tau:
        cum.append(cum[-1] + v)
    cum[-1] = 1.0
    span = [sum(w * tau[k] for k, w in o) for o in ov]
    good = [i for i, s in enumerate(span) if s >= 0.1]
    if not good:
        return cum, len(pairs), basis, {}, None
    ratios = [pairs[i][2] / span[i] for i in good]
    med = statistics.median(ratios)
    keep = [i for i, r in zip(good, ratios) if 0.4 * med <= r <= 2.5 * med]
    cells = defaultdict(lambda: [0.0, 0.0, 0, set()])
    allc = [0.0, 0.0, 0, set()]
    for i in keep:
        for c in (cells[keys[i]], allc):
            c[0] += pairs[i][2]
            c[1] += span[i]
            c[2] += 1
            c[3].add(pairs[i][4])
    t_all = allc[0] / allc[1] if allc[1] > 0 else None
    return cum, len(pairs), basis, cells, t_all


def cell_value(c):
    sec, span, n, days = c
    if n >= MIN_PAIRS and len(days) >= MIN_DAYS and span >= MIN_SPAN and span > 0:
        return {"sec": int(round(sec / span)), "n": n}
    return None


# ------------------------------------------------------------------ model
def load_summaries(odir, last_n):
    files = sorted(glob.glob(os.path.join(odir, "*.json")))
    if last_n:
        files = files[-last_n:]
    return [read_json(f) for f in files]


def build_model(summaries, lines, stops, seferler, built_at, version=None, only_days=None):
    pairs = defaultdict(list)
    rounds = samples = 0
    first = last = None
    round_hours = defaultdict(lambda: defaultdict(int))
    for s in summaries:
        rounds += s["rounds"]
        samples += s["samples"]
        if s["first"] and (first is None or s["first"] < first):
            first = s["first"]
        if s["last"] and (last is None or s["last"] > last):
            last = s["last"]
        for d, hs in s["round_hours"].items():
            for h, c in hs.items():
                round_hours[d][int(h)] += c
        for hid, rec in s["lines"].items():
            if hid in lines and rec["h"] == lines[hid]["hash"]:
                pairs[hid].extend(tuple(p) for p in rec["pairs"])
    day_list = sorted(round_hours)
    cov = {dtp: {"days": 0, "hours": []} for dtp in DAY_TYPES}
    hour_days = defaultdict(set)
    for d in day_list:
        dtp = day_type(dt.date.fromisoformat(d))
        cov[dtp]["days"] += 1
        for h in round_hours[d]:
            hour_days[(dtp, h)].add(d)
    for (dtp, h), ds in hour_days.items():
        cov[dtp]["hours"].append(h)
    for c in cov.values():
        c["hours"].sort()
    out_lines = []
    total_cells = suff_cells = with_est = tpairs = 0
    for hid in sorted(lines, key=lambda h: (lines[h]["code"], lines[h]["dir"])):
        ln = lines[hid]
        rt, basis_tt = route_type_for(ln, seferler.get(ln["code"], []))
        cum, pn, basis, cells, t_all = estimate_line(ln, pairs.get(hid, [])) if len(ln["stops"]) >= 3 else ([0.0, 1.0][:len(ln["stops"])], 0, "geometry", {}, None)
        trip = {}
        any_c = False
        for dtp in DAY_TYPES:
            arr = []
            for h in range(24):
                c = cells.get((dtp, h)) if cells else None
                v = cell_value(c) if c else None
                total_cells += 1 if c else 0
                if v:
                    suff_cells += 1
                    any_c = True
                arr.append(v)
            trip[dtp] = arr if any(arr) else None
        if any_c:
            with_est += 1
        tpairs += pn
        o = {"id": ln["id"], "dir": ln["dir"], "code": ln["code"], "name": ln["name"], "dirName": ln["dirName"],
             "stops": ln["stops"], "route_type": rt, "route_type_basis": basis_tt}
        if ln["shape"]:
            o["shape"] = ln["shape"]
        o["est"] = {"cum": [round(x, 4) for x in cum], "profile_n": pn, "profile_basis": basis, "trip": trip}
        out_lines.append(o)
    used_stops = {i for l in lines.values() for i in l["stops"]}
    model = {
        "schema": SCHEMA,
        "model_version": version or "",
        "built_at": built_at,
        "tz": "Europe/Istanbul",
        "source": {"days": len(day_list), "day_list": day_list, "rounds": rounds, "samples": samples, "pairs": tpairs,
                   "first": first, "last": last, "coverage": cov,
                   "notes": "Hafta içi/cumartesi/pazar ayrımı takvim gününe göredir; resmî tatiller ayrı işlenmez."},
        "stops": [stops[i] for i in sorted(used_stops)],
        "timetables": seferler,
        "lines": out_lines,
    }
    stats = {"days": len(day_list), "day_list": day_list, "rounds": rounds, "samples": samples, "pairs": tpairs,
             "first": first, "last": last, "coverage": cov,
             "cells": {"observed": total_cells, "sufficient": suff_cells},
             "lines": {"total": len(out_lines), "with_estimates": with_est}}
    return model, stats


def write_atomic(path, data):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def emit(model, stats, odir, prev_meta_path=None):
    """Modeli yaz. Kalite kapısı: önceki modelin tahminli hatlarının %80'inden azı kalırsa eski model durur."""
    raw = json.dumps(model, ensure_ascii=False, separators=(",", ":"), sort_keys=False).encode("utf-8")
    model["model_version"] = model["model_version"] or "%s-%s" % (model["built_at"][:10].replace("-", ""), hashlib.sha256(raw).hexdigest()[:8])
    raw = json.dumps(model, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    gz = gzip.compress(raw, 9, mtime=0)
    meta = {"schema": SCHEMA, "model_version": model["model_version"], "sha256": hashlib.sha256(gz).hexdigest(),
            "size_bytes": len(gz), "raw_size_bytes": len(raw), "file": "offline-model.json.gz",
            "built_at": model["built_at"], "generator": GENERATOR}
    meta.update(stats)
    if prev_meta_path and os.path.exists(prev_meta_path):
        prev = read_json(prev_meta_path)
        old = prev.get("lines", {}).get("with_estimates", 0)
        if old and stats["lines"]["with_estimates"] < 0.8 * old:
            raise SystemExit("KALITE KAPISI: tahminli hat sayisi %d < %%80 x %d; eski model korunuyor" % (stats["lines"]["with_estimates"], old))
    os.makedirs(odir, exist_ok=True)
    write_atomic(os.path.join(odir, "offline-model.json.gz"), gz)
    write_atomic(os.path.join(odir, "model-meta.json"), json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8"))
    return meta


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ doğrulama
def predicted_trip_seconds(est_line, day, hour, cells_override=None):
    arr = est_line["est"]["trip"].get(day)
    c = arr[hour] if arr else None
    return c["sec"] if c else None


def validate(vdir, sdir):
    lines, stops, seferler = load_static(sdir)
    files = data_files(vdir)
    raw_by_date = {}
    for f in files:
        raw_by_date[os.path.basename(f).split(".")[0]] = summarize_file(f, lines)
    dates = sorted(d for d in raw_by_date if raw_by_date[d]["lines"])
    report = []
    for held in dates:
        train = [raw_by_date[d] for d in dates if d != held]
        if not train:
            continue
        model, stats = build_model(train, lines, stops, seferler, "2000-01-01T00:00:00Z")
        by_id = {str(l["id"]): l for l in model["lines"]}
        errs, errs_pool, cover_n, total_n, gaps = [], [], 0, 0, []
        for hid, rec in raw_by_date[held]["lines"].items():
            ml = by_id.get(hid)
            if not ml or rec["h"] != lines[hid]["hash"]:
                continue
            cum = ml["est"]["cum"]
            pooled = [c["sec"] for arr in ml["est"]["trip"].values() if arr for c in arr if c]
            tp = statistics.median(pooled) if pooled else None
            for u1, u2, gap, mid, ordn in rec["pairs"]:
                total_n += 1
                d = dt.date.fromordinal(ordn)
                arr = ml["est"]["trip"].get(day_type(d))
                c = arr[mid // 60] if arr else None
                def frac(u):
                    i = int(u)
                    i = min(i, len(cum) - 2)
                    return cum[i] + (u - i) * (cum[i + 1] - cum[i])
                span = frac(u2) - frac(u1)
                if c:
                    cover_n += 1
                    errs.append((c["sec"] * span - gap) / 60.0)
                    gaps.append(gap / 60.0)
                    if tp:
                        errs_pool.append((tp * span - gap) / 60.0)
        def summ(e):
            if not e:
                return None
            a = [abs(x) for x in e]
            return {"n": len(e), "mae_dk": round(statistics.mean(a), 2), "medae_dk": round(statistics.median(a), 2),
                    "bias_dk": round(statistics.mean(e), 2), "ort_olculen_sure_dk": round(statistics.mean(gaps), 2), "bagil_hata": round(sum(abs(x) for x in e) / sum(gaps), 3), "p90_dk": round(sorted(a)[int(0.9 * (len(a) - 1))], 2)}
        report.append({"held_out": held, "train_days": [d for d in dates if d != held], "pairs_total": total_n,
                       "pairs_covered": cover_n, "coverage": round(cover_n / total_n, 3) if total_n else None,
                       "cell_model": summ(errs), "pooled_baseline": summ(errs_pool)})
    return report, route_type_evidence(vdir, lines, seferler)


def route_type_evidence(vdir, lines, seferler):
    """routeType <-> yön eşlemesi kanıtı: iki hipotezden hangisi tarife kalkışlarına gözlemleri daha iyi oturtur."""
    obs = defaultdict(list)
    for f in data_files(vdir):
        with open_text(f) as fh:
            for raw in fh:
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                ln = lines.get(str(r["h"]))
                if not ln:
                    continue
                t = parse_time(r.get("t") or r["tur"])
                if t.weekday() > 4:
                    continue
                for v in r.get("v") or []:
                    u = position(ln, v.get("o"), v.get("s"), v.get("la"), v.get("lo"))
                    if u is not None:
                        obs[str(r["h"])].append((t.hour * 60 + t.minute, u / (len(ln["stops"]) - 1)))
    bycode = defaultdict(list)
    for hid, ln in lines.items():
        bycode[ln["code"]].append(hid)

    def sched(e):
        return [int(x[:2]) * 60 + int(x[3:5]) for x in e["lineSchedules"].get("WORKDAY", [])]

    def best(points, S):
        if not S or not points:
            return None
        b = 1e9
        for T in range(10, 121, 3):
            err = sum(min(abs(m - T * f - s) for s in S) for m, f in points) / len(points)
            b = min(b, err)
        return b

    a_wins = b_wins = ties = 0
    for code, hids in bycode.items():
        tt = {e["routeType"]: e for e in seferler.get(code, [])}
        dirs = {lines[h]["dir"]: h for h in hids}
        if set(dirs) != {1, 2} or set(tt) != {1, 2} or sched(tt[1]) == sched(tt[2]):
            continue
        if min(len(obs[dirs[1]]), len(obs[dirs[2]])) < 8:
            continue
        A = (best(obs[dirs[1]], sched(tt[1])) or 1e9) + (best(obs[dirs[2]], sched(tt[2])) or 1e9)
        B = (best(obs[dirs[1]], sched(tt[2])) or 1e9) + (best(obs[dirs[2]], sched(tt[1])) or 1e9)
        if A < B - 1:
            a_wins += 1
        elif B < A - 1:
            b_wins += 1
        else:
            ties += 1
    return {"hipotez_A_routeType_esittir_yon": a_wins, "hipotez_B_ters": b_wins, "belirsiz": ties}


# ------------------------------------------------------------------ komutlar
def cmd_ozet(a, lines):
    os.makedirs(a.ozet, exist_ok=True)
    for f in data_files(a.veri):
        name = os.path.basename(f).split(".")[0] + ".json"
        out = os.path.join(a.ozet, name)
        if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(f) and not a.zorla:
            continue
        write_atomic(out, json.dumps(summarize_file(f, lines), separators=(",", ":")).encode("utf-8"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("komut", choices=["uret", "ozet", "model", "dogrula"], nargs="?", default="uret")
    ap.add_argument("--veri")
    ap.add_argument("--statik", required=True)
    ap.add_argument("--ozet")
    ap.add_argument("--cikti")
    ap.add_argument("--gun-sayisi", type=int, default=28)
    ap.add_argument("--model-surumu")
    ap.add_argument("--uretim-zamani")
    ap.add_argument("--onceki-meta")
    ap.add_argument("--zorla", action="store_true")
    a = ap.parse_args(argv)
    out = sys.stdout.buffer
    lines, stops, seferler = load_static(a.statik)
    if a.komut == "dogrula":
        rep, ev = validate(a.veri, a.statik)
        out.write(json.dumps({"katlar": rep, "route_type_kaniti": ev}, ensure_ascii=False, indent=1).encode("utf-8") + b"\n")
        return 0
    if a.komut in ("uret", "ozet"):
        ozet = a.ozet or tempfile.mkdtemp(prefix="maras-ozet-")
        a.ozet = ozet
        cmd_ozet(a, lines)
        if a.komut == "ozet":
            return 0
    if not a.cikti:
        ap.error("--cikti gerekli")
    model, stats = build_model(load_summaries(a.ozet, a.gun_sayisi), lines, stops, seferler,
                               a.uretim_zamani or now_iso(), a.model_surumu)
    meta = emit(model, stats, a.cikti, a.onceki_meta)
    out.write(json.dumps({k: meta[k] for k in ("model_version", "sha256", "size_bytes", "days", "samples", "cells", "lines")},
                         ensure_ascii=False).encode("utf-8") + b"\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
