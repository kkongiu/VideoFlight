#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rileva automaticamente lo sfasamento (offset) tra video GoPro e telemetria.

Idea: il momento in cui il motore si avvia è udibile nel video (salita netta
dell'energia audio) e corrisponde al comando di throttle nel CSV. L'offset è
la differenza tra i due istanti.

  offset = t_audio_avvio_motore - t_csv_impennata_throttle

Uso:
  python3 detect_offset.py --video GOPR2113.MP4 --csv telemetria.csv [--max-sec 30]

Stampa diagnostica e l'ultima riga "OFFSET 3.2". Con --quiet stampa solo il numero.
Solo librerie standard (niente numpy): gira su qualsiasi Python 3.
"""
import argparse
import array
import csv
import math
import subprocess
import sys
from datetime import datetime

COL_TIME = 1
COL_THR = 29   # colonna "Thr" del CSV Pocket/SkyLog


def percentile(values, q):
    s = sorted(values)
    if not s:
        return 0.0
    return s[int(q / 100.0 * (len(s) - 1))]


def read_audio_rms(video, max_sec=30.0, win=0.1):
    """Decodifica l'audio (mono 8 kHz) e restituisce [(t, rms)] per finestra."""
    cmd = ["ffmpeg", "-v", "error", "-i", video,
           "-t", f"{max_sec:.1f}", "-ac", "1", "-ar", "8000",
           "-f", "s16le", "pipe:1"]
    data = subprocess.run(cmd, capture_output=True).stdout
    if not data:
        return []
    a = array.array("h", data[:len(data) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    sr = 8000
    n = int(win * sr)
    out = []
    for i in range(0, len(a) - n + 1, n):
        chunk = a[i:i + n]
        s = sum(v * v for v in chunk)
        rms = math.sqrt(s / n)
        out.append((i / sr, rms))
    return out


def detect_audio_start(rms, need=6):
    """Trova l'istante in cui l'audio diventa sostenutamente forte (motore)."""
    if not rms:
        return None
    vals = [v for _, v in rms]
    noise = percentile(vals, 15)
    thr = max(noise * 6.0, noise + 500.0)
    for i in range(len(vals) - need + 1):
        if all(v > thr for v in vals[i:i + need]):
            return rms[i][0]
    for i, v in enumerate(vals):
        if v > thr:
            return rms[i][0]
    return None


def load_throttle(csv_path):
    """Restituisce [(elapsed_s, thr)] dal CSV."""
    rows = []
    t0 = None
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        next(reader, None)
        for r in reader:
            if len(r) <= COL_THR:
                continue
            try:
                t = datetime.strptime(r[COL_TIME], "%H:%M:%S.%f")
                thr = float(r[COL_THR])
            except (ValueError, IndexError):
                continue
            if t0 is None:
                t0 = t
            rows.append(((t - t0).total_seconds(), thr))
    return rows


def detect_throttle(rows):
    """Trova l'istante in cui il throttle sale e resta su (avvio motore)."""
    if not rows:
        return None
    vals = [v for _, v in rows]
    lo, hi = min(vals), max(vals)
    thr = max(0.05 * (hi - lo), 10.0)
    for i in range(len(rows) - 2):
        t, v = rows[i]
        if v >= thr and all(rows[i + j][1] >= thr * 0.5 for j in (1, 2)):
            return t
    return None


def detect_offset(video, csv_path, max_sec=30.0):
    """Ritorna (offset, info) dove offset è float o None se non rilevabile."""
    info = {}
    rms = read_audio_rms(video, max_sec)
    t_audio = detect_audio_start(rms)
    info["audio_start"] = t_audio
    info["noise"] = percentile([v for _, v in rms], 15) if rms else 0.0

    rows = load_throttle(csv_path)
    t_thr = detect_throttle(rows)
    info["throttle_start"] = t_thr
    info["thr_range"] = (min((v for _, v in rows), default=0),
                         max((v for _, v in rows), default=0))

    if t_audio is None or t_thr is None:
        return None, info
    return round(t_audio - t_thr, 1), info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--max-sec", type=float, default=30.0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    offset, info = detect_offset(args.video, args.csv, args.max_sec)

    if offset is None:
        if not args.quiet:
            sys.stderr.write("ERRORE: offset non rilevabile "
                             "(audio o throttle non trovati)\n")
            sys.stderr.write(f"  info: {info}\n")
        sys.exit(1)

    if args.quiet:
        print(f"{offset:.1f}")
    else:
        print(f"Avvio motore (audio)   : {info['audio_start']:.2f} s")
        print(f"Impennata throttle (CSV): {info['throttle_start']:.2f} s")
        print(f"Rumore di fondo audio   : {info['noise']:.0f}")
        print(f"Range throttle         : {info['thr_range']}")
        print(f"OFFSET {offset}")


if __name__ == "__main__":
    main()
