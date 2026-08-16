#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Interfaccia grafica (Tkinter) per enhance_video.sh + add_telemetry.py."""
import os
import json
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from fractions import Fraction

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENHANCE = os.path.join(SCRIPT_DIR, "enhance_video.sh")
CONFIG_PATH = os.path.expanduser("~/.config/videoflight.json")
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

PHASES = {
    "analisi movimento": "Analisi movimento",
    "overlay": "Generazione overlay",
    "elaborazione": "Elaborazione video",
    "completato": "Completato",
}
# Ordine delle fasi (per l'indicatore a step)
STEPS = ["analisi movimento", "overlay", "elaborazione"]
STEP_NAMES = ["1 Analisi", "2 Overlay", "3 Elaborazione"]
STEP_COLORS = {"done": "#b7e1cd", "active": "#ffe082", "pending": "#e2e2e2"}

DISP_W = 480  # larghezza di visualizzazione del player

try:
    from PIL import Image, ImageDraw, ImageFont, ImageTk
    HAS_PIL = True
except Exception:
    HAS_PIL = False


def _load_font(size):
    for path in ("/System/Library/Fonts/Helvetica.ttc",
                 "/System/Library/Fonts/SFNS.ttf",
                 "/Library/Fonts/Arial.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def ffprobe(*args):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", *args], text=True).strip()
    return out


def fmt_eta(seconds):
    if seconds < 0 or seconds != seconds or seconds > 1e6:
        return "…"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h} h {m:02d} min"
    return f"{m}:{s:02d} min"


def fmt_time(seconds):
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Video GoPro + Telemetria")
        self.geometry("900x940")
        self.minsize(720, 760)

        # stato elaborazione
        self.proc = None
        self.progress_file = None
        self.start_time = 0.0
        self.mode = ""
        self.last_out = ""
        self.log_queue = queue.Queue()

        # stato player
        self.player_proc = None
        self.player_path = None
        self.player_w = DISP_W
        self.player_h = int(270)
        self.pdur = 0.0
        self.ppos = 0.0
        self.pdt = 1.0 / 24.0
        self.decode_playing = False
        self.finished = False
        self.eof_pending = False
        self.dragging = False
        self.frame_queue = queue.Queue(maxsize=10)
        self.play_event = threading.Event()
        self.current_img = None  # mantiene vivo il PhotoImage mostrato

        self.video_var = tk.StringVar()
        self.csv_var = tk.StringVar()
        self.offset_var = tk.StringVar(value="3.5")
        self.preview_var = tk.StringVar(value="30")
        self.out_var = tk.StringVar()
        self.open_ext = tk.BooleanVar(value=False)

        self.last_dir = ""
        self._load_config()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._poll)
        self.after(40, self._vtick)

    # ---------- costruzione UI ----------
    def _build(self):
        pad = {"padx": 6, "pady": 4}
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        self._file_row(frm, 0, "Video GoPro (MP4)", self.video_var, self._browse_video)
        self._file_row(frm, 1, "Telemetria CSV (opzionale)", self.csv_var, self._browse_csv)

        opts = ttk.Frame(frm)
        opts.grid(row=2, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(opts, text="Anticipo video (s):").pack(side="left")
        ttk.Spinbox(opts, from_=-10, to=60, increment=0.5, width=6,
                    textvariable=self.offset_var).pack(side="left", padx=(4, 4))
        self.btn_detect = ttk.Button(opts, text="Rileva", width=7,
                                     command=self._detect_offset)
        self.btn_detect.pack(side="left", padx=(0, 16))
        ttk.Label(opts, text="Durata anteprima (s):").pack(side="left")
        ttk.Spinbox(opts, from_=5, to=300, increment=5, width=6,
                    textvariable=self.preview_var).pack(side="left", padx=4)

        self._file_row(frm, 3, "File di output (vuoto = automatico)", self.out_var, self._browse_out)

        # bottoni
        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=2, sticky="w", **pad)
        self.btn_preview = ttk.Button(btns, text="Anteprima rapida",
                                      command=lambda: self._start("preview"))
        self.btn_preview.pack(side="left", padx=(0, 6))
        self.btn_full = ttk.Button(btns, text="Converti video completo",
                                   command=lambda: self._start("full"))
        self.btn_full.pack(side="left", padx=6)
        self.btn_cancel = ttk.Button(btns, text="Annulla", state="disabled",
                                     command=self._cancel)
        self.btn_cancel.pack(side="left", padx=6)
        ttk.Checkbutton(frm, text="Apri anche in QuickTime",
                        variable=self.open_ext).grid(
            row=5, column=0, columnspan=2, sticky="w", **pad)

        # ----- pannello avanzamento (controlli) -----
        prog = ttk.LabelFrame(frm, text="Avanzamento", padding=8)
        prog.grid(row=6, column=0, columnspan=2, sticky="ew", **pad)

        # indicatore a step
        steps = ttk.Frame(prog)
        steps.pack(fill="x", pady=(0, 6))
        self.step_lbls = []
        for i, name in enumerate(STEP_NAMES):
            if i:
                ttk.Label(steps, text=" → ").pack(side="left")
            lbl = tk.Label(steps, text=name, width=15, padx=4, pady=2,
                           bg=STEP_COLORS["pending"], fg="#444",
                           font=("Helvetica", 11, "bold"))
            lbl.pack(side="left")
            self.step_lbls.append(lbl)
        self._set_steps(0)

        # barre
        barr = ttk.Frame(prog)
        barr.pack(fill="x", pady=2)
        ttk.Label(barr, text="Totale:").pack(side="left")
        self.bar_total = ttk.Progressbar(barr, maximum=100, length=330)
        self.bar_total.pack(side="left", padx=6)
        self.lbl_total = ttk.Label(barr, text="0%", width=5)
        self.lbl_total.pack(side="left")

        barb = ttk.Frame(prog)
        barb.pack(fill="x", pady=2)
        ttk.Label(barb, text="Fase:  ").pack(side="left")
        self.bar_phase = ttk.Progressbar(barb, maximum=100, length=330)
        self.bar_phase.pack(side="left", padx=6)
        self.lbl_phase = ttk.Label(barb, text="—", width=22)
        self.lbl_phase.pack(side="left")

        self.lbl_speed = ttk.Label(prog, text="", anchor="w")
        self.lbl_speed.pack(fill="x", pady=(2, 0))

        # ----- player video integrato -----
        playf = ttk.LabelFrame(frm, text="Anteprima video", padding=8)
        playf.grid(row=7, column=0, columnspan=2, sticky="nsew", **pad)
        frm.rowconfigure(8, weight=1)

        self.video_panel = tk.Label(playf)
        self.video_panel.pack(fill="x")
        self._show_placeholder("Nessuna anteprima.\nGenera un'anteprima per vederla qui.")

        trans = ttk.Frame(playf)
        trans.pack(fill="x", pady=(6, 0))
        self.btn_play = ttk.Button(trans, text="▶ Riproduci", width=12,
                                   state="disabled", command=self._toggle_play)
        self.btn_play.pack(side="left")
        self.btn_restart = ttk.Button(trans, text="⏮ Riavvolgi", width=11,
                                      state="disabled", command=self._restart)
        self.btn_restart.pack(side="left", padx=6)
        self.slider = ttk.Scale(trans, from_=0, to=100, length=420,
                                command=lambda v: None)
        self.slider.pack(side="left", padx=6, fill="x", expand=True)
        self.slider.configure(state="disabled")
        self.slider.bind("<ButtonPress-1>", self._slider_press)
        self.slider.bind("<ButtonRelease-1>", self._slider_release)
        self.lbl_time = ttk.Label(trans, text="0:00 / 0:00", width=14)
        self.lbl_time.pack(side="left")

        # ----- log -----
        logf = ttk.LabelFrame(frm, text="Registro", padding=6)
        logf.grid(row=8, column=0, columnspan=2, sticky="nsew", **pad)
        self.txt = tk.Text(logf, height=10, width=70, state="disabled",
                           wrap="word", bg="#111", fg="#ddd",
                           font=("Menlo", 10))
        self.txt.pack(fill="both", expand=True)
        frm.rowconfigure(8, weight=1)
        frm.columnconfigure(1, weight=1)

    def _file_row(self, parent, row, label, var, browse):
        pad = {"padx": 6, "pady": 4}
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", **pad)
        ent = ttk.Entry(parent, textvariable=var, width=52)
        ent.grid(row=row, column=1, sticky="ew", padx=4, pady=4)
        ttk.Button(parent, text="…", width=3, command=browse).grid(
            row=row, column=2, padx=(0, 4))

    # ---------- dialoghi ----------
    def _browse_video(self):
        p = filedialog.askopenfilename(
            title="Scegli il video GoPro",
            initialdir=self.last_dir or None,
            filetypes=[("Video MP4", "*.mp4 *.MP4"), ("Tutti i file", "*")])
        if p:
            self.video_var.set(p)
            self.last_dir = os.path.dirname(p)
            if not self.out_var.get():
                base = os.path.splitext(p)[0]
                self.out_var.set(base + "_enhanced.mp4")

    def _browse_csv(self):
        p = filedialog.askopenfilename(
            title="Scegli la telemetria (CSV)",
            initialdir=self.last_dir or None,
            filetypes=[("CSV", "*.csv"), ("Tutti i file", "*")])
        if p:
            self.csv_var.set(p)
            self.last_dir = os.path.dirname(p)

    def _browse_out(self):
        p = filedialog.asksaveasfilename(
            title="File di output",
            initialdir=self.last_dir or None,
            defaultextension=".mp4",
            filetypes=[("Video MP4", "*.mp4")])
        if p:
            self.out_var.set(p)

    # ---------- impostazioni persistenti ----------
    def _load_config(self):
        cfg = {}
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
        except Exception:
            return
        self.last_dir = cfg.get("last_dir", "") or ""
        # video/csv solo se il file esiste ancora
        for key, var in (("video", self.video_var), ("csv", self.csv_var)):
            v = cfg.get(key, "")
            if isinstance(v, str) and v and os.path.isfile(v):
                var.set(v)
        for key, var, default in (("offset", self.offset_var, "3.5"),
                                  ("preview_sec", self.preview_var, "30")):
            v = cfg.get(key)
            if isinstance(v, (int, float, str)) and str(v).strip():
                var.set(str(v).strip())
            else:
                var.set(default)
        if isinstance(cfg.get("open_ext"), bool):
            self.open_ext.set(cfg["open_ext"])
        geo = cfg.get("geometry")
        if isinstance(geo, str) and geo:
            try:
                self.geometry(geo)
            except Exception:
                pass

    def _save_config(self, save_geometry=False):
        cfg = {
            "video": self.video_var.get().strip(),
            "csv": self.csv_var.get().strip(),
            "offset": self.offset_var.get().strip(),
            "preview_sec": self.preview_var.get().strip(),
            "open_ext": bool(self.open_ext.get()),
            "last_dir": self.last_dir,
        }
        if save_geometry and self.winfo_viewable():
            cfg["geometry"] = self.geometry()
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w") as f:
                json.dump(cfg, f, indent=2)
        except OSError:
            pass

    # ---------- rilevamento automatico offset ----------
    def _detect_offset(self):
        video = self.video_var.get().strip()
        csv = self.csv_var.get().strip()
        if not video or not os.path.isfile(video):
            messagebox.showerror("Errore", "Seleziona prima un video GoPro.")
            return
        if not csv or not os.path.isfile(csv):
            messagebox.showerror("Errore", "Seleziona prima la telemetria (CSV).")
            return
        self.btn_detect.configure(state="disabled")
        self.detect_queue = queue.Queue()

        def worker():
            offset = None
            info = {}
            msg = "Rilevamento offset non riuscito."
            try:
                import detect_offset
                offset, info = detect_offset.detect_offset(video, csv)
                if offset is not None:
                    msg = (f"Offset rilevato: {offset}s "
                           f"(motore audio {info.get('audio_start'):.2f}s, "
                           f"throttle {info.get('throttle_start'):.2f}s)")
            except Exception as e:
                msg = f"Errore rilevamento: {e}"
            self.detect_queue.put((offset, msg))

        threading.Thread(target=worker, daemon=True).start()
        self.after(100, self._detect_poll)

    def _detect_poll(self):
        if not hasattr(self, "detect_queue"):
            return
        try:
            offset, msg = self.detect_queue.get_nowait()
        except queue.Empty:
            self.after(100, self._detect_poll)
            return
        self._detect_done(offset, msg)

    def _detect_done(self, offset, msg):
        self.btn_detect.configure(state="normal")
        if offset is not None:
            self.offset_var.set(f"{offset:.1f}")
        self._log(msg + "\n")

    # ---------- avanzamento (step + barre) ----------
    def _set_steps(self, idx):
        for i, lbl in enumerate(self.step_lbls):
            if i < idx or idx >= len(STEPS):
                lbl.configure(bg=STEP_COLORS["done"], fg="#0b6b3a")
            elif i == idx and idx < len(STEPS):
                lbl.configure(bg=STEP_COLORS["active"], fg="#5d4400")
            else:
                lbl.configure(bg=STEP_COLORS["pending"], fg="#888")

    def _apply_progress(self, phase, local, overall, speed):
        overall = min(max(overall, 0), 100)
        local = min(max(local, 0), 100)
        self.bar_total["value"] = overall
        self.lbl_total["text"] = f"{overall}%"
        self.bar_phase["value"] = local
        stp = PHASES.get(phase, phase)
        self.lbl_phase["text"] = stp
        if phase in STEPS:
            self._set_steps(STEPS.index(phase) + 1)
        else:
            self._set_steps(len(STEPS))  # completato
        # etichetta velocità + tempo stimato
        txt = ""
        if overall > 0 and overall < 100 and self.start_time:
            el = time.time() - self.start_time
            txt = f"tempo trascorso {fmt_eta(el)} · restano ~{fmt_eta(el * (100 - overall) / overall)}"
        if speed:
            txt += (f"\nvelocità {speed}" if txt else f"velocità {speed}")
        self.lbl_speed["text"] = txt

    # ---------- esecuzione ----------
    def _start(self, mode):
        if self.proc and self.proc.poll() is None:
            messagebox.showwarning("Occupato", "Elaborazione già in corso.")
            return
        video = self.video_var.get().strip()
        if not video:
            messagebox.showerror("Errore", "Seleziona un video GoPro.")
            return
        if not os.path.isfile(video):
            messagebox.showerror("Errore", "Video non trovato:\n" + video)
            return
        csv = self.csv_var.get().strip()
        if csv and not os.path.isfile(csv):
            messagebox.showerror("Errore", "CSV non trovato:\n" + csv)
            return
        if not os.path.isfile(ENHANCE):
            messagebox.showerror("Errore", f"enhance_video.sh non trovato:\n{ENHANCE}")
            return

        out = self.out_var.get().strip()
        if not out:
            base = os.path.splitext(video)[0]
            out = base + ("_preview.mp4" if mode == "preview" else "_enhanced.mp4")

        cmd = ["bash", ENHANCE, video, "--out", out]
        if csv:
            offset = self.offset_var.get().strip() or "3.5"
            cmd += ["--csv", csv, "--offset", offset]
        if mode == "preview":
            dur = self.preview_var.get().strip() or "30"
            cmd += ["--preview", dur]

        fd, pf = tempfile.mkstemp(prefix="gopro_prog_", suffix=".txt")
        os.close(fd)
        cmd += ["--progress-file", pf]
        self.progress_file = pf
        self.mode = mode
        self.last_out = out
        self.start_time = time.time()

        self._clear_log()
        self._log("$ " + " ".join(cmd) + "\n")
        self.bar_total["value"] = 0
        self.lbl_total["text"] = "0%"
        self.bar_phase["value"] = 0
        self.lbl_phase["text"] = "Avvio…"
        self.lbl_speed["text"] = ""
        self._set_steps(0)
        self.btn_preview["state"] = "disabled"
        self.btn_full["state"] = "disabled"
        self.btn_cancel["state"] = "normal"

        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True)
        threading.Thread(target=self._reader, daemon=True).start()
        self._save_config()

    def _kill(self, proc):
        """Termina il processo e tutto il suo gruppo (figli inclusi)."""
        if proc is None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    def _reader(self):
        for line in self.proc.stdout:
            self.log_queue.put(line)

    def _poll(self):
        if self.proc is None:
            return
        # flush log
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._log_append(line)

        # progress file (4 righe: fase, %locale, %globale, velocità)
        phase, local, overall, speed = self._read_progress()

        if self.proc.poll() is None:
            if phase:
                self._apply_progress(phase, local, overall, speed)
            self.after(200, self._poll)
        else:
            self._finish(self.proc.returncode)

    def _read_progress(self):
        if not self.progress_file or not os.path.isfile(self.progress_file):
            return "", 0, 0, ""
        try:
            with open(self.progress_file) as f:
                lines = [l.strip() for l in f.read().splitlines() if l.strip()]
            if not lines:
                return "", 0, 0, ""
            phase = lines[0]
            local = int(lines[1]) if len(lines) > 1 and lines[1].lstrip("-").isdigit() else 0
            overall = int(lines[2]) if len(lines) > 2 and lines[2].lstrip("-").isdigit() else local
            speed = lines[3] if len(lines) > 3 else ""
            return phase, local, overall, speed
        except OSError:
            return "", 0, 0, ""

    def _finish(self, code):
        self.btn_preview["state"] = "normal"
        self.btn_full["state"] = "normal"
        self.btn_cancel["state"] = "disabled"
        ok = (code == 0)
        if ok:
            self.bar_total["value"] = 100
            self.lbl_total["text"] = "100%"
            self.bar_phase["value"] = 100
            self.lbl_phase["text"] = "Completato"
            self.lbl_speed["text"] = ""
            self._set_steps(len(STEPS))
        else:
            self.lbl_phase["text"] = f"ERRORE (codice {code})"

        pf = self.progress_file
        self.progress_file = None
        out = getattr(self, "last_out", "") or self.out_var.get().strip()

        if ok:
            self._log_append("\n– Elaborazione terminata correttamente.\n")
            if os.path.isfile(out):
                # carica nel player integrato
                self._player_open(out)
                if self.open_ext.get():
                    try:
                        subprocess.Popen(["open", out])
                    except OSError:
                        pass
            else:
                self._log_append(f"\nAVVISO: output non trovato: {out}\n")
        else:
            self._log_append(f"\n– Elaborazione fallita.\n")
            messagebox.showerror("Errore", "Elaborazione fallita. Vedi il registro.")

        try:
            if pf:
                os.unlink(pf)
        except OSError:
            pass
        if self.proc:
            self.proc = None

    # ---------- player video integrato ----------
    def _show_pil(self, img, placeholder=False):
        tkimg = ImageTk.PhotoImage(img)
        self.current_img = tkimg  # riferimento obbligatorio
        self.video_panel.configure(image=tkimg)
        if placeholder:
            self.video_panel.configure(text="")

    def _show_placeholder(self, text):
        if not HAS_PIL:
            self.video_panel.configure(text=text)
            return
        w, h = DISP_W, self.player_h
        img = Image.new("RGB", (w, h), (40, 44, 52))
        d = ImageDraw.Draw(img)
        fnt = _load_font(18)
        lines = text.splitlines()
        total = len(lines) * 26
        y = (h - total) // 2
        for line in lines:
            bbox = d.textbbox((0, 0), line, font=fnt)
            tw = bbox[2] - bbox[0]
            d.text(((w - tw) // 2, y), line, fill=(170, 175, 185), font=fnt)
            y += 26
        self._show_pil(img)

    def _probe_video(self, path):
        dur = float(ffprobe("-show_entries", "format=duration",
                            "-of", "csv=p=0", path) or 0)
        w, h, fps = ffprobe("-select_streams", "v:0",
                            "-show_entries", "stream=width,height,r_frame_rate",
                            "-of", "csv=p=0", path).split(",")
        w, h = int(w), int(h)
        try:
            fps = float(Fraction(fps))
        except Exception:
            fps = 30.0
        if not fps or fps <= 0:
            fps = 30.0
        dh = int(h * DISP_W / max(w, 1)) // 2 * 2
        dh = max(dh, 2)
        return dur, DISP_W, dh, fps

    def _player_stop(self):
        self.decode_playing = False
        self.finished = True
        try:
            self.play_event.clear()
        except Exception:
            pass
        if self.player_proc is not None:
            try:
                self.player_proc.terminate()
            except Exception:
                pass
            try:
                self.player_proc.wait(timeout=2)
            except Exception:
                try:
                    self.player_proc.kill()
                except Exception:
                    pass
            self.player_proc = None
        self._drain_frames()

    def _drain_frames(self):
        while True:
            try:
                self.frame_queue.get_nowait()
            except queue.Empty:
                break

    def _player_open(self, path):
        self._player_stop()
        if not HAS_PIL:
            self._log_append("AVVISO: Pillow non disponibile, player disattivato.\n")
            return
        try:
            dur, w, h, fps = self._probe_video(path)
        except Exception:
            self._log_append(f"ERRORE lettura video: {path}\n")
            return
        self.player_path = path
        self.player_w, self.player_h = w, h
        self.pdur = dur or 0
        self.ppos = 0.0
        self.pdt = 1.0 / min(fps, 30.0)
        self.finished = False
        self.eof_pending = False
        self.decode_playing = True
        self.play_event.set()

        self.btn_play.configure(state="normal", text="❚❚ Pausa")
        self.btn_restart.configure(state="normal")
        self.slider.configure(state="normal", to=max(int(dur), 1))
        self.slider.set(0)
        self._update_transport()

        self._spawn_decoder(path, 0.0)
        threading.Thread(target=self._decoder, daemon=True).start()
        self._log_append(f"– Anteprima caricata nel player.\n")

    def _spawn_decoder(self, path, seek):
        dw, dh = self.player_w, self.player_h
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               "-ss", f"{seek:.2f}", "-i", path,
               "-map", "0:v:0", "-vf", f"scale={dw}:{dh}",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        self.player_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

    def _decoder(self):
        proc = self.player_proc
        nbytes = self.player_w * self.player_h * 3
        while True:
            self.play_event.wait()
            if self.player_proc is not proc or not self.decode_playing:
                return
            try:
                block = proc.stdout.read(nbytes)
            except Exception:
                if self.player_proc is proc:
                    self.eof_pending = True
                return
            if not block or len(block) != nbytes:
                if self.player_proc is proc:
                    self.eof_pending = True
                return
            img = Image.frombytes("RGB", (self.player_w, self.player_h), block)
            if self.player_proc is not proc or not self.decode_playing:
                return
            try:
                self.frame_queue.put_nowait(img)
            except queue.Full:
                try:
                    self.frame_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.frame_queue.put_nowait(img)
                except queue.Full:
                    pass
            time.sleep(self.pdt)

    def _toggle_play(self):
        if not self.player_path:
            return
        if self.finished:
            self._seek(0.0)
            return
        if self.decode_playing:
            self.decode_playing = False
            try:
                self.play_event.clear()
            except Exception:
                pass
            self.btn_play.configure(text="▶ Riprendi")
        else:
            self._drain_frames()
            self.decode_playing = True
            self.play_event.set()
            self.btn_play.configure(text="❚❚ Pausa")

    def _restart(self):
        if self.player_path:
            self._seek(0.0)

    def _seek(self, t):
        if not self.player_path:
            return
        self._drain_frames()
        if self.player_proc is not None:
            try:
                self.player_proc.terminate()
            except Exception:
                pass
            self.player_proc = None
        t = max(0.0, min(t, self.pdur))
        self.ppos = t
        self.finished = False
        self.decode_playing = True
        self.play_event.set()
        self.btn_play.configure(state="normal", text="❚❚ Pausa")
        self._spawn_decoder(self.player_path, t)
        threading.Thread(target=self._decoder, daemon=True).start()
        self._update_transport()

    def _slider_press(self, _e):
        self.dragging = True

    def _slider_release(self, _e):
        self.dragging = False
        try:
            self._seek(float(self.slider.get()))
        except Exception:
            pass

    def _update_transport(self):
        self.lbl_time["text"] = f"{fmt_time(self.ppos)} / {fmt_time(self.pdur)}"
        if not self.dragging:
            try:
                self.slider.set(min(self.ppos, self.pdur))
            except Exception:
                pass

    def _vtick(self):
        if self.player_path and self.decode_playing and not self.finished:
            frames = []
            try:
                while True:
                    frames.append(self.frame_queue.get_nowait())
            except queue.Empty:
                pass
            if frames:
                img = frames[-1]
                self._show_pil(img)
                self.ppos += self.pdt * len(frames)
                if self.pdur and self.ppos > self.pdur:
                    self.ppos = self.pdur
                self._update_transport()
        if self.eof_pending:
            self.eof_pending = False
            self.decode_playing = False
            self.finished = True
            try:
                self.play_event.clear()
            except Exception:
                pass
            self.btn_play.configure(text="▶ Rivedi")
            self.ppos = self.pdur
            self._update_transport()
        self.after(40, self._vtick)

    # ---------- cancellazione ----------
    def _cancel(self):
        if self.proc and self.proc.poll() is None:
            self._kill(self.proc)
            self._log("\n– Annullato dall'utente.\n")

    # ---------- log ----------
    def _clear_log(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def _log(self, text):
        self.txt.configure(state="normal")
        self.txt.insert("end", text)
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _log_append(self, line):
        clean = ANSI_RE.sub("", line).replace("\r", " ")
        if not clean.strip():
            return
        self._log(clean)

    def _on_close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("Uscire?", "Elaborazione in corso. Interrompere?"):
                return
            self._kill(self.proc)
        self._player_stop()
        self._save_config(save_geometry=True)
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()