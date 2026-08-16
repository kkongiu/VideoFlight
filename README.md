# VideoFlight

Miglioramento e stabilizzazione di video GoPro (prima serie) con overlay di
telemetria di volo (OSD + minimappa) e interfaccia grafica di gestione.

## Cosa fa

Pipeline se in un unico passaggio sequenziale (senza tagli tra segmenti):

1. **Stabilizzazione** movimento (vidstab, 2 passaggi: detect + transform)
2. **Miglioramento immagine**: contrasto/brightness/saturazione/gamma (eq),
   ripristino livelli bianco/nero (colorlevels), denoise (hqdn3d), sharpening
   (unsharp)
3. **Normalizzazione audio** (loudnorm)
4. **Overlay telemetria** (opzionale, abilitato con `--csv`):
   - pannello **OSD** in basso a sinistra: velocità, altitudine, rateo
     verticale, rotta e batteria (dati smussati con media mobile)
   - **minimappa** in basso a destra con posizione corrente (spline
     Catmull-Rom sui punti GPS), freccia di direzione, freccia nord e barra
     di scala

## Struttura del progetto

| File | Descrizione |
|---|---|
| `enhance_video.sh` | Pipeline principale (bash/ffmpeg). Stabilizzazione, miglioramento colore, audio, overlay, modalità preview e file di progresso. |
| `add_telemetry.py` | Genera gli overlay `osd.mov` + `minimap.mp4` a partire dal CSV (formato RadioMaster/SkyLog), con cache delle tile OSM in `~/.cache/osm_tiles`. Supporta anche la composizione autonoma (`--out`). |
| `detect_offset.py` | Rileva automaticamente l'offset video↔telemetria: confronta l'avvio del motore nell'audio del video con l'impennata di throttle nel CSV. |
| `gui.py` | Interfaccia grafica Tkinter: selezione video/CSV, offset (con pulsante "Rileva"), anteprima rapida, conversione completa, barre di avanzamento, registro, apertura del risultato. |
| `avvia_gui.command` | Launcher della GUI (doppio clic in Finder o da terminale): sceglie automaticamente un Python con Tkinter 8.6+ / 9.0. |

## Requisiti

- **ffmpeg** (con filtri `vidstabdetect`/`vidstabtransform`, `loudnorm`,
  encoder `h264_videotoolbox` o `libx264`): `brew install ffmpeg`
- **python3** con Tkinter 8.6+/9.0 per la GUI:
  `brew install python-tk@3.13`
- Librerie Python per la GUI e per `add_telemetry.py`: `pillow`, `requests`

Setup consigliato (virtualenv della GUI, usato da `avvia_gui.command`):

```
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/python -m pip install pillow
```

Nota: la pipeline (`enhance_video.sh`) usa il `python3` di pyenv (dotato di
`pillow` e `requests`) per generare gli overlay; la GUI usa il venv
`.venv` (Tk 9.0 + Pillow) per il player video integrato.

## Uso

### GUI (consigliato)

```
./avvia_gui.command
```

oppure doppio clic su `avvia_gui.command` in Finder.

### Da terminale

```
# Anteprima rapida (primi 30 s) con overlay
./enhance_video.sh GOPR2113.MP4 \
  --csv telemetria.csv --offset 3.5 --preview 30 --out preview.mp4

# Conversione completa con overlay
./enhance_video.sh GOPR2113.MP4 \
  --csv telemetria.csv --offset 3.5 --out finale.mp4

# Solo overlay (senza miglioramento video)
python3 add_telemetry.py --video GOPR2113.MP4 --csv telemetria.csv \
  --offset 3.5 --out prova.mp4

# Solo generazione overlay in una cartella (per pipeline/GPU)
python3 add_telemetry.py --video GOPR2113.MP4 --csv telemetria.csv \
  --render /tmp/overlay --dur 476.8
```

### Opzioni di `enhance_video.sh`

| Opzione | Descrizione |
|---|---|
| `--csv FILE` | Telemetria (RadioMaster/Betaflight/SkyLog) per OSD + minimappa |
| `--offset SEC` | Anticipo del video rispetto al CSV (default `3.5`); con `auto` lo rileva automaticamente |
| `--preview SEC` | Genera solo i primi SEC secondi (anteprima veloce) |
| `--out FILE` | Nome del file di output |
| `--progress-file F` | Scrive fase/percentuale/velocità su F (per la GUI) |
| `-h, --help` | Aiuto |

## Parametri principali (in cima a `enhance_video.sh`)

- `LENS_CORRECT=false` — correzione fisheye **disattivata di default**: il
  modello radiale di ffmpeg tende a "spalmare" la correzione su tutta
  l'immagine invece che ai bordi. Se attivata, usare `LENS_K1` basso (~0.08).
- `VIDSTAB_SHAKINESS=8` / `VIDSTAB_SMOOTHING=20` — intensità stabilizzazione
- `EQ_CONTRAST=1.12`, `EQ_SATURATION=1.12`, `EQ_GAMMA=1.0`
- `ENCODER="h264_videotoolbox"` — encoder hardware macOS; bitrate automatico
  uguale alla sorgente (con videotoolbox `-q:v` è inaffidabile)
- `AUDIO_NORMALIZE=true`
- `MINIMAP_TARGET=240` — larghezza della minimappa in overlay (px)
- `PARALLEL=false` — sequenziale di default (evita piccoli tagli tra
  segmenti); si può forzare `true` per velocizzare

## Stato attuale (in sviluppo)

Funzionante:

- stabilizzazione + miglioramento colore/audio in un solo passaggio
- overlay OSD + minimappa con posizione fluida (spline)
- anteprima rapida, file di progresso, GUI completa
- **barre di avanzamento come controlli GUI**: indicatore a 3 step
  (Analisi → Overlay → Elaborazione), barra complessiva e barra della fase
  attiva, velocità e tempo stimato/trascorso
- **player video integrato nella GUI**: al termine di anteprima o
  conversione il risultato si carica direttamente nella finestra, con
  Play/Pausa, Riavvolgi, slider di posizione e timestamp (decodifica via
  ffmpeg, visualizzazione con Pillow)
- la GUI è stata adattata a Tk 9.0 (il Tk 8.5 del Python pyenv non è
  compatibile con macOS 15: usare comunque `avvia_gui.command`)

Limiti noti:

- I file video (`*.mp4`) e la telemetria (`*.csv`) sono esclusi dal
  repository: i video superano il limite GitHub di 100 MB e i CSV contengono
  dati di volo personali. Restano solo in locale.
- `optzoom` con zoom fisso: il bordo possibile accanto alla stabilizzazione
  è gestito con l'opzione `zoom=1:optzoom=1`.