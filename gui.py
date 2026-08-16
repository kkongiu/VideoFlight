#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Interfaccia grafica (Tkinter) per enhance_video.sh + add_telemetry.py."""
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENHANCE = os.path.join(SCRIPT_DIR, "enhance_video.sh")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

PHASES = {
    "analisi movimento": "Analisi movimento",
    "overlay": "Generazione overlay",
    "elaborazione": "Elaborazione video",
    "completato": "Completato",
}

W = 60
H = 14


def fmt_eta(seconds):
    if seconds < 0 or not seconds < 10**6:
        return "…"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h} h {m:02d} min"
    return f"{m}:{s:02d} min"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Video GoPro + Telemetria")
        self.geometry("760x600")
        self.minsize(640, 500)

        self.proc = None
        self.progress_file = None
        self.start_time = 0.0
        self.mode = ""
        self.log_queue = queue.Queue()

        self.video_var = tk.StringVar()
        self.csv_var = tk.StringVar()
        self.offset_var = tk.StringVar(value="3.5")
        self.preview_var = tk.StringVar(value="30")
        self.out_var = tk.StringVar()
        self.open_after = tk.BooleanVar(value=True)

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- costruzione UI ----------
    def _build(self):
        pad = {"padx": 6, "pady": 4}
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        self._file_row(frm, 0, "Video GoPro (MP4)", self.video_var,
                       self._browse_video, "…")
        self._file_row(frm, 1, "Telemetria CSV (opzionale)", self.csv_var,
                       self._browse_csv, "…")

        # offset + durata preview
        opts = ttk.Frame(frm)
        opts.grid(row=2, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(opts, text="Anticipo video (s):").pack(side="left")
        ttk.Spinbox(opts, from_=-10, to=60, increment=0.5, width=6,
                    textvariable=self.offset_var).pack(side="left", padx=(4, 16))
        ttk.Label(opts, text="Durata anteprima (s):").pack(side="left")
        ttk.Spinbox(opts, from_=5, to=300, increment=5, width=6,
                    textvariable=self.preview_var).pack(side="left", padx=4)

        self._file_row(frm, 3, "File di output (vuoto = automatico)", self.out_var,
                       self._browse_out, "...")

        # bottoni
        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=2, sticky="w", **pad)
        self.btn_preview = ttk.Button(btns, text="Anteprima rapida (10 s)",
                                      command=lambda: self._start("preview"))
        self.btn_preview.pack(side="left", padx=(0, 6))
        self.btn_full = ttk.Button(btns, text="Converti video completo",
                                   command=lambda: self._start("full"))
        self.btn_full.pack(side="left", padx=6)
        self.btn_cancel = ttk.Button(btns, text="Annulla", state="disabled",
                                     command=self._cancel)
        self.btn_cancel.pack(side="left", padx=6)
        ttk.Checkbutton(frm, text="Apri il risultato al termine",
                        variable=self.open_after).grid(
            row=5, column=0, columnspan=2, sticky="w", **pad)

        # barra di avanzamento
        prog = ttk.Frame(frm)
        prog.grid(row=6, column=0, columnspan=2, sticky="ew", **pad)
        self.prog_bar = ttk.Progressbar(prog, maximum=100, length=450)
        self.prog_bar.pack(side="left")
        self.prog_lbl = tk.Label(prog, text="Pronto", anchor="w", width=46)
        self.prog_lbl.pack(side="left", padx=8)

        # log
        logf = ttk.Frame(frm)
        logf.grid(row=7, column=0, columnspan=2, sticky="nsew", **pad)
        ttk.Label(logf, text="Registro:").pack(anchor="w")
        self.txt = tk.Text(logf, height=H, width=W, state="disabled",
                           wrap="word", bg="#111", fg="#ddd",
                           font=("Menlo", 10))
        self.txt.pack(fill="both", expand=True)
        frm.rowconfigure(7, weight=1)
        frm.columnconfigure(1, weight=1)

    def _file_row(self, parent, row, label, var, browse, btn_txt):
        pad = {"padx": 6, "pady": 4}
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", **pad)
        ent = ttk.Entry(parent, textvariable=var, width=52)
        ent.grid(row=row, column=1, sticky="ew", padx=4, pady=4)
        ttk.Button(parent, text=btn_txt, width=3, command=browse).grid(
            row=row, column=2, padx=(0, 4))

    # ---------- dialoghi ----------
    def _browse_video(self):
        p = filedialog.askopenfilename(
            title="Scegli il video GoPro",
            filetypes=[("Video MP4", "*.mp4 *.MP4"), ("Tutti i file", "*")])
        if p:
            self.video_var.set(p)
            if not self.out_var.get():
                base = os.path.splitext(p)[0]
                self.out_var.set(base + "_enhanced.mp4")

    def _browse_csv(self):
        p = filedialog.askopenfilename(
            title="Scegli la telemetria (CSV)",
            filetypes=[("CSV", "*.csv"), ("Tutti i file", "*")])
        if p:
            self.csv_var.set(p)

    def _browse_out(self):
        p = filedialog.asksaveasfilename(
            title="File di output",
            defaultextension=".mp4",
            filetypes=[("Video MP4", "*.mp4")])
        if p:
            self.out_var.set(p)

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
        self.start_time = time.time()

        self._clear_log()
        self._log("$ " + " ".join(cmd) + "\n")
        self.prog_bar["value"] = 0
        self.prog_lbl["text"] = "Avvio…"
        self.btn_preview["state"] = "disabled"
        self.btn_full["state"] = "disabled"
        self.btn_cancel["state"] = "normal"

        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        threading.Thread(target=self._reader, daemon=True).start()
        self.after(200, self._poll)

    def _reader(self):
        for line in self.proc.stdout:
            self.log_queue.put(line)

    def _poll(self):
        if not self.proc:
            return
        # flush log
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._log_append(line)

        # progress file
        pct, phase, speed = self._read_progress()
        if phase:
            pct = max(int(pct or 0), 0)
            pct = min(pct, 100)
            self.prog_bar["value"] = pct
            name = PHASES.get(phase, phase)
            txt = f"{name}  —  {pct}%"
            if pct > 0 and pct < 100 and self.start_time:
                el = time.time() - self.start_time
                eta = el * (100 - pct) / pct
                txt += f"   (restano ~{fmt_eta(eta)})"
            if speed:
                txt += f"   [{speed}]"
            self.prog_lbl["text"] = txt

        if self.proc.poll() is None:
            self.after(200, self._poll)
        else:
            self._finish(self.proc.returncode, pct)

    def _read_progress(self):
        if not self.progress_file or not os.path.isfile(self.progress_file):
            return 0, "", ""
        try:
            with open(self.progress_file) as f:
                lines = [l.strip() for l in f.read().splitlines() if l.strip()]
            phase = lines[0] if lines else ""
            pct = lines[1] if len(lines) > 1 else "0"
            speed = lines[2] if len(lines) > 2 else ""
            return pct, phase, speed
        except OSError:
            return 0, "", ""

    def _finish(self, code, last_pct):
        self.btn_preview["state"] = "normal"
        self.btn_full["state"] = "normal"
        self.btn_cancel["state"] = "disabled"
        ok = (code == 0)
        if ok:
            self.prog_bar["value"] = 100
            self.prog_lbl["text"] = "Completato"
        else:
            self.prog_lbl["text"] = f"ERRORE (codice {code})"

        # auto-apertura del risultato
        pf_saved = self.progress_file
        self.progress_file = None
        if ok and self.open_after.get():
            out = self._last_out()
            if out and os.path.isfile(out):
                try:
                    subprocess.Popen(["open", out])
                except OSError:
                    pass
        try:
            if pf_saved:
                os.unlink(pf_saved)
        except OSError:
            pass
        if not ok:
            messagebox.showerror("Errore",
                                 "Elaborazione fallita. Vedi il registro.")

    def _last_out(self):
        return self.out_var.get().strip()

    def _cancel(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
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
            if not messagebox.askyesno("Uscire?",
                                       "Elaborazione in corso. Interrompere?"):
                return
            self.proc.terminate()
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()