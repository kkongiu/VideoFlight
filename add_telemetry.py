#!/usr/bin/env python3
"""Aggiunge OSD telemetria + minimappa (stile Google Earth) a un video.

Legge un CSV di log (RadioMaster Pocket / SkyLog), genera:
  - osd.ass    : testo telemetria (velocità, quota, vario, rotta)
  - minimap.mp4: minimappa OSM con tracciato + marcatore che avanza
e li compone sul video con ffmpeg.

Uso:
  python3 add_telemetry.py --video GOPR2113_enhanced.mp4 \
      --csv "POCKET-....csv" --offset 4 --out GOPR2113_final.mp4

  --offset = secondi di anticipo del video rispetto al CSV (default 3.5)
"""

import argparse
import csv
import math
import os
import subprocess
import sys
import tempfile
from datetime import datetime

import requests
from PIL import Image, ImageDraw, ImageFont

# ------------------------------------------------------------------
# Configurazione
# ------------------------------------------------------------------
FPS = 10                 # fps della minimappa
MINIMAP_TARGET = 240     # larghezza finale della minimappa in overlay (px)
OSD_FPS = 2              # fps del pannello OSD (i dati sono a 2 Hz)
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_HEADERS = {"User-Agent": "telemetry-overlay/1.0 (personal use)"}
TILE_CACHE = os.path.expanduser("~/.cache/osm_tiles")

# Colonne del CSV (indici fissi per il formato Pocket/SkyLog)
COL_TIME = 1
COL_VSPD = 13      # VSpd(m/s)
COL_ALTB = 14      # Alt(m) barometrica
COL_GPS = 15       # "lat lon"
COL_GSPD = 16      # GSpd(kmh)
COL_HDG = 17       # Hdg(°)
COL_BAT = 23       # Bat%(%)

# ------------------------------------------------------------------
# Utilità
# ------------------------------------------------------------------
def parse_time(tstr):
    return datetime.strptime(tstr, "%H:%M:%S.%f")

def ffprobe_duration(video):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", video])
    return float(out.strip())

def lonlat_to_px(lon, lat, z):
    """Coordinate globali (float) in Web Mercator a zoom z."""
    n = 2.0 ** z
    x = (lon + 180.0) / 360.0 * n * 256.0
    lat_r = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n * 256.0
    return x, y

def get_tile(z, x, y):
    os.makedirs(TILE_CACHE, exist_ok=True)
    path = os.path.join(TILE_CACHE, f"{z}_{x}_{y}.png")
    if not os.path.exists(path):
        url = TILE_URL.format(z=z, x=x, y=y)
        r = requests.get(url, headers=TILE_HEADERS, timeout=20)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    return Image.open(path).convert("RGB")

def fmt_ass_time(sec):
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    if cs == 100:
        s += 1
        cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

def find_font():
    for p in ["/System/Library/Fonts/Helvetica.ttc",
              "/Library/Fonts/Arial.ttf",
              "/System/Library/Fonts/SFNS.ttf"]:
        if os.path.exists(p):
            return p
    return None

# ------------------------------------------------------------------
# Caricamento dati
# ------------------------------------------------------------------
def load_track(csv_path):
    rows = []
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        header = next(reader)
        for r in reader:
            if len(r) <= COL_GPS:
                continue
            gps = r[COL_GPS].strip()
            if not gps:
                continue
            parts = gps.split()
            if len(parts) != 2:
                continue
            try:
                t = parse_time(r[COL_TIME])
                lat = float(parts[0])
                lon = float(parts[1])
                gspd = float(r[COL_GSPD]) if r[COL_GSPD] else 0.0
                altb = float(r[COL_ALTB]) if r[COL_ALTB] else 0.0
                vspd = float(r[COL_VSPD]) if r[COL_VSPD] else 0.0
                hdg = float(r[COL_HDG]) if r[COL_HDG] else 0.0
                bat = float(r[COL_BAT]) if r[COL_BAT] else 0.0
            except (ValueError, IndexError):
                continue
            rows.append(dict(t=t, lat=lat, lon=lon, gspd=gspd,
                             alt=altb, vspd=vspd, hdg=hdg, bat=bat))
    if not rows:
        sys.exit("ERRORE: nessuna riga valida nel CSV")
    t0 = rows[0]["t"]
    for r in rows:
        r["e"] = (r["t"] - t0).total_seconds()
    smooth_telemetry(rows)
    return rows, t0


def smooth_telemetry(rows, window=5):
    """Smussa i valori (media mobile centrata) per evitare numeri a salti."""
    n = len(rows)
    half = window // 2

    def ma(key):
        out = []
        for i in range(n):
            s = 0.0
            c = 0
            for j in range(max(0, i - half), min(n, i + half + 1)):
                s += rows[j][key]
                c += 1
            out.append(s / c)
        return out

    for key in ("gspd", "alt", "vspd"):
        out = ma(key)
        for i in range(n):
            rows[i][key] = out[i]

    # heading: media circolare
    out = []
    for i in range(n):
        sx = sy = 0.0
        for j in range(max(0, i - half), min(n, i + half + 1)):
            a = math.radians(rows[j]["hdg"])
            sx += math.cos(a)
            sy += math.sin(a)
        out.append(math.degrees(math.atan2(sy, sx)) % 360.0)
    for i in range(n):
        rows[i]["hdg"] = out[i]

# ------------------------------------------------------------------
# Minimappa
# ------------------------------------------------------------------
def build_map_image(rows):
    lats = [r["lat"] for r in rows]
    lons = [r["lon"] for r in rows]
    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)
    # padding 30%
    pad_lat = (lat_max - lat_min) * 0.30 + 0.0003
    pad_lon = (lon_max - lon_min) * 0.30 + 0.0003
    lat_min -= pad_lat; lat_max += pad_lat
    lon_min -= pad_lon; lon_max += pad_lon

    # scegli zoom: bbox larghezza ~600px
    z = 15
    for cand in range(15, 20):
        w = (lon_max - lon_min) * 256.0 * (2 ** cand) / 360.0
        if w >= 480:
            z = cand
            break

    x0, y0 = lonlat_to_px(lon_min, lat_min, z)
    x1, y1 = lonlat_to_px(lon_max, lat_max, z)
    px_min, py_min = min(x0, x1), min(y0, y1)
    px_max, py_max = max(x0, x1), max(y0, y1)

    tx0 = int(math.floor(px_min / 256))
    tx1 = int(math.floor(px_max / 256))
    ty0 = int(math.floor(py_min / 256))
    ty1 = int(math.floor(py_max / 256))

    W = (tx1 - tx0 + 1) * 256
    H = (ty1 - ty0 + 1) * 256
    img = Image.new("RGB", (W, H), (240, 240, 240))
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            tile = get_tile(z, tx, ty)
            img.paste(tile, ((tx - tx0) * 256, (ty - ty0) * 256))

    # ritaglia alla bbox
    left = int(px_min - tx0 * 256)
    top = int(py_min - ty0 * 256)
    right = int(px_max - tx0 * 256)
    bottom = int(py_max - ty0 * 256)
    img = img.crop((left, top, right, bottom))

    def to_px(lon, lat):
        gx, gy = lonlat_to_px(lon, lat, z)
        return gx - px_min, gy - py_min

    return img, to_px


def draw_arrow(draw, cx, cy, heading_deg, size, color):
    """Triangolo orientato secondo heading (0=N/up, 90=E/right)."""
    a = math.radians(heading_deg)
    # direzione sullo schermo: up=-y, right=+x
    dx = math.sin(a)
    dy = -math.cos(a)
    tip = (cx + dx * size, cy + dy * size)
    perp = (-dy, dx)
    base_l = (cx - dx * size * 0.5 + perp[0] * size * 0.55,
              cy - dy * size * 0.5 + perp[1] * size * 0.55)
    base_r = (cx - dx * size * 0.5 - perp[0] * size * 0.55,
              cy - dy * size * 0.5 - perp[1] * size * 0.55)
    draw.polygon([tip, base_l, base_r], fill=color)


def catmull_rom(p0, p1, p2, p3, t):
    t2 = t * t
    t3 = t2 * t
    return 0.5 * ((2.0 * p1) + (-p0 + p2) * t +
                  (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2 +
                  (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t3)


def render_minimap(rows, out_path, dur, limit=None):
    img, to_px = build_map_image(rows)
    W, H = img.size

    # punti chiave: solo dove cambia il GPS (si aggiorna ~ogni 7s)
    keys = []  # (elapsed, px, py, hdg)
    prev_gps = None
    for r in rows:
        g = (round(r["lat"], 6), round(r["lon"], 6))
        if g != prev_gps:
            px, py = to_px(r["lon"], r["lat"])
            keys.append((r["e"], px, py, r["hdg"]))
            prev_gps = g

    # scala in metri per pixel (per la barra scala)
    lats = [r["lat"] for r in rows]
    lons = [r["lon"] for r in rows]
    dlat = (max(lats) - min(lats)) * 111320
    dlon = (max(lons) - min(lons)) * 111320 * math.cos(math.radians(sum(lats) / len(lats)))
    span_m = math.hypot(dlat, dlon)
    span_px = math.hypot(keys[-1][1] - keys[0][1], keys[-1][2] - keys[0][2]) or 1.0
    m_per_px = span_m / span_px

    font = find_font()
    fnt = ImageFont.truetype(font, 28) if font else None

    def sample_pos(e):
        """Posizione liscia (spline Catmull-Rom sui punti GPS)."""
        if e <= keys[0][0]:
            return keys[0][1], keys[0][2]
        if e >= keys[-1][0]:
            return keys[-1][1], keys[-1][2]
        i = 0
        while i < len(keys) - 1 and e > keys[i + 1][0]:
            i += 1
        p0 = keys[max(0, i - 1)]
        p1 = keys[i]
        p2 = keys[i + 1]
        p3 = keys[min(len(keys) - 1, i + 2)]
        span = p2[0] - p1[0]
        t = (e - p1[0]) / span if span > 0 else 0.0
        px = catmull_rom(p0[1], p1[1], p2[1], p3[1], t)
        py = catmull_rom(p0[2], p1[2], p2[2], p3[2], t)
        return px, py

    def sample_hdg(e):
        """Rotta interpolata linearmente sui campioni (2Hz)."""
        if e <= rows[0]["e"]:
            return rows[0]["hdg"]
        if e >= rows[-1]["e"]:
            return rows[-1]["hdg"]
        i = int(e * 2)
        i = max(0, min(i, len(rows) - 2))
        a, b = rows[i], rows[i + 1]
        frac = (e - a["e"]) / (b["e"] - a["e"]) if b["e"] > a["e"] else 0.0
        da = (b["hdg"] - a["hdg"] + 180.0) % 360.0 - 180.0
        return (a["hdg"] + da * frac) % 360.0

    def frame_at(t_video):
        f = img.copy()
        d = ImageDraw.Draw(f)
        e = t_video - OFFSET
        if e < 0 or not keys:
            return f
        cx, cy = sample_pos(e)
        hdg = sample_hdg(e)
        # marcatore (nessun tracciato)
        r = 9
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255),
                  outline=(255, 120, 0), width=3)
        draw_arrow(d, cx, cy, hdg, 18, (0, 0, 0))
        draw_arrow(d, cx, cy, hdg, 14, (255, 255, 255))
        # freccia nord
        draw_arrow(d, 30, 30, 0, 16, (200, 0, 0))
        if fnt:
            d.text((34, 38), "N", font=fnt, fill=(200, 0, 0))
        # barra scala (50 m)
        bar_px = 50 / m_per_px
        d.line([(30, H - 30), (30 + bar_px, H - 30)], fill=(0, 0, 0), width=4)
        d.line([(30, H - 30), (30, H - 26)], fill=(0, 0, 0), width=3)
        d.line([(30 + bar_px, H - 30), (30 + bar_px, H - 26)], fill=(0, 0, 0), width=3)
        if fnt:
            d.text((30, H - 62), "50 m", font=fnt, fill=(0, 0, 0))
        return f

    # rende i frame e li passa a ffmpeg via pipe
    n_frames = int(dur * FPS)
    if limit:
        n_frames = min(n_frames, limit)

    # dimensioni pari per yuv420p
    W = W // 2 * 2
    H = H // 2 * 2

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-pix_fmt", "yuv420p", out_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(n_frames):
        t = i / FPS
        frame = frame_at(t)
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        sys.exit("ERRORE: codifica minimappa fallita")


# ------------------------------------------------------------------
# OSD (pannello telemetria renderizzato con PIL, stile avionico)
# ------------------------------------------------------------------
def render_osd(rows, out_path, dur):
    W, H = 336, 72
    mono = "/System/Library/Fonts/Menlo.ttc"
    if not os.path.exists(mono):
        mono = find_font()
    fnt_label = ImageFont.truetype(mono, 9)
    fnt_value = ImageFont.truetype(mono, 20)
    fnt_unit = ImageFont.truetype(mono, 8)

    def frame_at(t_video):
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, W - 1, H - 1], radius=10,
                            fill=(15, 18, 30, 185),
                            outline=(255, 140, 0, 90), width=1)
        e = t_video - OFFSET
        if e < 0:
            return img
        i = int(e * 2)
        i = max(0, min(i, len(rows) - 1))
        r = rows[i]
        cells = [
            ("SPD", f"{r['gspd']:.0f}", "km/h", (255, 255, 255)),
            ("ALT", f"{r['alt']:.0f}", "m", (255, 255, 255)),
            ("VS", f"{r['vspd']:+.1f}", "m/s",
             (120, 225, 120) if r["vspd"] >= 0 else (255, 150, 120)),
            ("HDG", f"{r['hdg']:.0f}", "\u00b0", (255, 255, 255)),
        ]
        n = len(cells)
        pad = 14
        cell_w = (W - 2 * pad) / n
        for k, (label, val, unit, color) in enumerate(cells):
            x = pad + k * cell_w
            d.text((x, 8), label, font=fnt_label, fill=(130, 140, 158, 255))
            d.text((x, 22), val, font=fnt_value, fill=color + (255,))
            d.text((x, 52), unit, font=fnt_unit, fill=(110, 120, 138, 255))
        return img

    n_frames = int(dur * OSD_FPS)
    W = W // 2 * 2
    H = H // 2 * 2
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}",
           "-r", str(OSD_FPS), "-i", "-",
           "-c:v", "qtrle", out_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(n_frames):
        t = i / OSD_FPS
        proc.stdin.write(frame_at(t).tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        sys.exit("ERRORE: codifica OSD fallita")


# ------------------------------------------------------------------
# Composizione finale
# ------------------------------------------------------------------
def composite(video, minimap, osd, out, with_map=True):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", video]
    if with_map:
        cmd += ["-i", minimap]
    cmd += ["-i", osd]
    if with_map:
        fc = (f"[1:v]scale={MINIMAP_TARGET}:-1,"
              "drawbox=x=0:y=0:w=iw:h=ih:color=white@0.85:t=2[m];"
              "[0:v][m]overlay=W-w-20:H-h-20[v0];"
              "[v0][2:v]overlay=20:H-h-20[v]")
    else:
        fc = "[0:v][1:v]overlay=20:H-h-20[v]"
    cmd += ["-filter_complex", fc,
            "-map", "[v]", "-map", "0:a",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-movflags", "+faststart", out]
    print("==> Composizione finale (overlay OSD + minimappa)...")
    subprocess.check_call(cmd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--offset", type=float, default=3.5,
                    help="secondi di anticipo del video rispetto al CSV")
    ap.add_argument("--out", default=None)
    ap.add_argument("--render", default=None, metavar="DIR",
                    help="genera solo osd.mov e minimap.mp4 in DIR, poi esce")
    ap.add_argument("--dur", type=float, default=0,
                    help="durata overlay in secondi (0 = durata video)")
    ap.add_argument("--no-map", action="store_true", help="salta la minimappa")
    ap.add_argument("--limit", type=int, default=0,
                    help="test: numero massimo di frame minimappa")
    args = ap.parse_args()

    global OFFSET
    OFFSET = args.offset

    if not os.path.exists(args.video):
        sys.exit(f"ERRORE: video non trovato: {args.video}")
    if not os.path.exists(args.csv):
        sys.exit(f"ERRORE: csv non trovato: {args.csv}")

    dur = ffprobe_duration(args.video)
    print(f"==> Video: {args.video} ({dur:.1f}s)")
    print(f"==> CSV: {args.csv}")

    rows, t0 = load_track(args.csv)
    print(f"==> {len(rows)} campioni, da {t0.time()} a {rows[-1]['t'].time()}")

    if args.render:
        os.makedirs(args.render, exist_ok=True)
        osd_path = os.path.join(args.render, "osd.mov")
        minimap_path = os.path.join(args.render, "minimap.mp4")
        overlay_dur = args.dur if args.dur > 0 else dur
        print("==> Genero pannello OSD (osd.mov)...")
        render_osd(rows, osd_path, overlay_dur)
        if not args.no_map:
            print("==> Genero minimappa (minimap.mp4)...")
            render_minimap(rows, minimap_path, overlay_dur, limit=args.limit)
        print("==> Overlay generati in:", args.render)
        return

    if not args.out:
        sys.exit("ERRORE: --out richiesto (oppure usa --render DIR)")

    tmp = tempfile.mkdtemp(prefix="telemetry_")
    try:
        osd_path = os.path.join(tmp, "osd.mov")
        minimap_path = os.path.join(tmp, "minimap.mp4")

        print("==> Genero pannello OSD (osd.mov)...")
        render_osd(rows, osd_path, dur)

        if not args.no_map:
            print("==> Genero minimappa (minimap.mp4)...")
            render_minimap(rows, minimap_path, dur, limit=args.limit)

        composite(args.video, minimap_path, osd_path, args.out,
                  with_map=not args.no_map)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"==> Fatto! Video finale: {args.out}")


if __name__ == "__main__":
    main()
