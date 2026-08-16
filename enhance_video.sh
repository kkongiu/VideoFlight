#!/usr/bin/env bash
set -euo pipefail

# Evita problemi di parsing dei decimali con locale italiano (awk/printf)
export LC_NUMERIC=C

usage() {
  cat <<'EOF'
Uso: ./enhance_video.sh INPUT [OUTPUT] [opzioni]

Stabilizza e migliora la resa di video GoPro (prima serie) con ffmpeg:
  - Correzione distorsione fisheye (opzionale, disattivata di default)
  - Stabilizzazione movimento (vidstab, 2 passaggi)
  - Contrasto/brightness/saturazione (eq)
  - Ripristino livelli bianco/nero (colorlevels)
  - Denoise (hqdn3d)
  - Sharpening (unsharp)
  - Normalizzazione audio (loudnorm)
  - Overlay telemetria OSD + minimappa (se --csv)
  - Elaborazione sequenziale in un solo passaggio (senza tagli tra segmenti)
  - Barra di avanzamento live

Opzioni:
  --csv FILE          telemetria CSV (RadioMaster/Betaflight) per OSD + minimappa
  --offset SEC        anticipo del video rispetto al CSV (default 3.5);
                      usa --offset auto per il rilevamento automatico
  --preview SEC       genera solo i primi SEC secondi (preview veloce)
  --out FILE          nome del file di output
  --progress-file F   scrive fase+percentuale (0-100) su F (per la GUI)
  -h, --help          mostra questo aiuto

Parametri regolabili nel file.
EOF
}

# ---------- PARAMETRI (regola a piacere) ----------
# Correzione distorsione lente (fisheye). false = disattivata.
# Nota: ffmpeg usa solo un modello radiale semplice (k1,k2) che NON riproduce fedelmente
# il fisheye GoPro: tende a "spalmare" la correzione su tutta l'immagine anziché solo ai
# bordi. Per questo è disattivata di default. Se la attivi, prova k1 più basso (es. 0.08).
LENS_CORRECT=false
# Lente GoPro: k1 ~0.10 (debole) ... 0.25 (forte), k2 di solito 0.05-0.10
LENS_K1=0.16
LENS_K2=0.06
# Zoom dopo la correzione lente (rimuove i bordi neri della distorsione).
# 0 = nessuno zoom (restano i bordi). 15 = consigliato. Aumenta se vedi ancora bordi neri.
LENS_ZOOM=15

# Stabilizzazione: shakiness 1 (poco) ... 10 (molto)
VIDSTAB_SHAKINESS=8
VIDSTAB_SMOOTHING=20

# Colore
EQ_CONTRAST=1.12
EQ_BRIGHTNESS=0.02
EQ_SATURATION=1.12
EQ_GAMMA=1.0

# Denoise (valori bassi = poco effetto)
DENOISE_LUMA=4
DENOISE_CHROMA=3
DENOISE_LUMA_T=6
DENOISE_CHROMA_T=4.5

# Sharpening
SHARP_LUMA=5:5:0.5:5:5:0.0

# Encoder video
#   - h264_videotoolbox : hardware (GPU), veloce. Usa -b:v (bitrate costante, dimensione prevedibile)
#   - libx264           : software, usa -preset/-crf (qualità costante)
ENCODER="h264_videotoolbox"
# Bitrate video in kbps. 0 = automatico (uguale alla sorgente). Es. 15000 per 15 Mbps.
# Nota: con videotoolbox NON usare -q:v, è inaffidabile (bitrate imprevedibile).
TARGET_BITRATE_KBPS=0

# Audio: true = normalizza volume (loudnorm)
AUDIO_NORMALIZE=true

# Overlay: larghezza della minimappa in overlay (px). Il pannello OSD ha dimensioni già fisse.
MINIMAP_TARGET=240

# Parallelismo (default: sequenziale, per non introdurre tagli tra i segmenti)
# Puoi forzare PARALLEL=true per velocizzare, ma potresti vedere piccoli tagli.
PARALLEL=false
JOBS=$(sysctl -n hw.ncpu 2>/dev/null || echo 4)
SEG_SECONDS=15   # durata di ogni segmento (solo se PARALLEL=true)
# -------------------------------------------------

# ---------- Parsing argomenti ----------
INPUT=""
OUTPUT=""
CSV=""
OFFSET=3.5
PREVIEW_SEC=0
PROGRESS_FILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --csv) CSV="$2"; shift 2 ;;
    --offset) OFFSET="$2"; shift 2 ;;
    --preview) PREVIEW_SEC="$2"; shift 2 ;;
    --out) OUTPUT="$2"; shift 2 ;;
    --progress-file) PROGRESS_FILE="$2"; shift 2 ;;
    -*) echo "ERRORE: opzione sconosciuta: $1" >&2; usage >&2; exit 1 ;;
    *) if [[ -z "$INPUT" ]]; then INPUT="$1"; shift
       elif [[ -z "$OUTPUT" ]]; then OUTPUT="$1"; shift
       else echo "ERRORE: troppi argomenti posizionali" >&2; exit 1; fi ;;
  esac
done

if [[ -z "$INPUT" ]]; then
  usage >&2
  exit 1
fi
if [[ ! -f "$INPUT" ]]; then
  echo "ERRORE: file non trovato: $INPUT" >&2
  exit 1
fi
if [[ -z "$OUTPUT" ]]; then
  OUTPUT="${INPUT%.*}_enhanced.mp4"
fi
ORIG_INPUT="$INPUT"
if [[ -n "$CSV" && ! -f "$CSV" ]]; then
  echo "ERRORE: csv non trovato: $CSV" >&2
  exit 1
fi
if [[ -n "$CSV" || -n "$PROGRESS_FILE" ]]; then
  if [[ "$PARALLEL" == "true" ]]; then
    echo "NOTA: overlay/progress richiedono la modalità sequenziale; forzo PARALLEL=false" >&2
    PARALLEL=false
  fi
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="$SCRIPT_DIR/add_telemetry.py"
DETECT_SCRIPT="$SCRIPT_DIR/detect_offset.py"
if [[ -n "$CSV" && ! -f "$PY_SCRIPT" ]]; then
  echo "ERRORE: add_telemetry.py non trovato accanto allo script: $PY_SCRIPT" >&2
  exit 1
fi
if [[ "$OFFSET" == "auto" && ! -f "$DETECT_SCRIPT" ]]; then
  echo "ERRORE: detect_offset.py non trovato: $DETECT_SCRIPT" >&2
  exit 1
fi
if [[ -n "$CSV" ]] && ! command -v python3 >/dev/null 2>&1; then
  echo "ERRORE: python3 richiesto per l'overlay (--csv)" >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
LOG="$WORKDIR/ffmpeg.log"

DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$INPUT")
DURATION="${DURATION:-0}"

# Preview: estrai solo i primi N secondi (clip locale)
if [[ "$PREVIEW_SEC" =~ ^[0-9]+$ ]] && [[ "$PREVIEW_SEC" -gt 0 ]]; then
  echo "==> Preview: uso i primi ${PREVIEW_SEC}s del video..."
  ffmpeg -y -hide_banner -loglevel error -t "$PREVIEW_SEC" -i "$INPUT" \
    -c copy "$WORKDIR/clip.mp4"
  INPUT="$WORKDIR/clip.mp4"
  DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$INPUT")
fi

# Dimensioni del video (per il ritaglio dopo la correzione lente)
W=$(ffprobe -v error -select_streams v:0 -show_entries stream=width -of csv=p=0 "$INPUT")
H=$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$INPUT")
W="${W:-1280}"; H="${H:-720}"

# Calcolo scale/crop per eliminare i bordi neri della lenscorrection
SCALE_W=$(( W * (100 + LENS_ZOOM) / 100 ))
SCALE_H=$(( H * (100 + LENS_ZOOM) / 100 ))
SCALE_W=$(( SCALE_W / 2 * 2 ))   # dimensioni pari (richieste da yuv420p)
SCALE_H=$(( SCALE_H / 2 * 2 ))
CROP_X=$(( (SCALE_W - W) / 2 ))
CROP_Y=$(( (SCALE_H - H) / 2 ))

# ----- Impostazione encoder -----
if [[ "$ENCODER" == "h264_videotoolbox" ]]; then
  if [[ "$TARGET_BITRATE_KBPS" -eq 0 ]]; then
    SRC_BITRATE=$(ffprobe -v error -select_streams v:0 -show_entries stream=bit_rate -of csv=p=0 "$INPUT")
    SRC_BITRATE="${SRC_BITRATE:-15000000}"
    TARGET_BITRATE_KBPS=$(( SRC_BITRATE / 1000 ))
  fi
  ENCODE_OPTS="-c:v h264_videotoolbox -b:v ${TARGET_BITRATE_KBPS}k"
else
  ENCODE_OPTS="-c:v libx264 -preset medium -crf 18"
fi

echo "==> Analizzo il video: $INPUT ($(echo "$DURATION" | awk '{printf "%.1f s", $1}'))"
ffprobe -v error -select_streams v:0 -show_entries stream=width,height,r_frame_rate -of csv=p=0 "$INPUT"

# ---------- Filtri (stringhe riusate) ----------
if [[ "$LENS_CORRECT" == "true" ]]; then
  PRE_FILTER="lenscorrection=cx=0.5:cy=0.5:k1=${LENS_K1}:k2=${LENS_K2},scale=${SCALE_W}:${SCALE_H}:flags=lanczos,crop=${W}:${H}:${CROP_X}:${CROP_Y},"
else
  PRE_FILTER=""
fi
COLOR_FILTER="eq=contrast=${EQ_CONTRAST}:brightness=${EQ_BRIGHTNESS}:saturation=${EQ_SATURATION}:gamma=${EQ_GAMMA},colorlevels=rimin=0.05:gimin=0.05:bimin=0.05:rimax=0.95:gimax=0.95:bimax=0.95,hqdn3d=${DENOISE_LUMA}:${DENOISE_CHROMA}:${DENOISE_LUMA_T}:${DENOISE_CHROMA_T},unsharp=${SHARP_LUMA}"

DETECT_FILTER="${PRE_FILTER}vidstabdetect=stepsize=6:shakiness=${VIDSTAB_SHAKINESS}:accuracy=9:fileformat=ascii:result="
TRANSFORM_FILTER="${PRE_FILTER}vidstabtransform=input="
TRANSFORM_TAIL=":zoom=1:optzoom=1:smoothing=${VIDSTAB_SMOOTHING},$COLOR_FILTER"

# ---------- Barra di avanzamento (sequenziale) ----------
PROG_BASE=0; PROG_RANGE=100
# Durata in microsecondi (intera) per calcolare la % senza awk ad ogni riga
DURATION_US=$(awk -v d="$DURATION" 'BEGIN{printf "%d", d * 1000000}')
[[ "$DURATION_US" -le 0 ]] && DURATION_US=1

# Progresso per la GUI: 4 righe => fase, %locale, %globale, velocità
write_progress() {
  [[ -n "$PROGRESS_FILE" ]] && printf '%s\n%d\n%d\n%s\n' "$1" "$2" "${3:-0}" "${4:-}" > "$PROGRESS_FILE"
}

progress() {
  local label="$1" pct=0 last_pct=-1 out_us="" speed=""
  while IFS= read -r line; do
    case "$line" in
      out_time_us=*)
        out_us="${line#out_time_us=}"
        if [[ "$out_us" =~ ^[0-9]+$ ]]; then
          pct=$(( out_us * 100 / DURATION_US ))
          [[ "$pct" -gt 100 ]] && pct=100
        fi
        ;;
      speed=*) speed="${line#speed=}" ;;
      progress=end) pct=100 ;;
    esac
    if [[ "$pct" -ne "$last_pct" ]]; then
      if [[ -n "$PROGRESS_FILE" ]]; then
        # Niente barra ASCII quando c'è la GUI: pulisce il registro
        local overall=$(( PROG_BASE + pct * PROG_RANGE / 100 ))
        [[ "$overall" -gt 100 ]] && overall=100
        write_progress "$label" "$pct" "$overall" "$speed"
      else
        local width=40 filled
        filled=$(( pct * width / 100 ))
        printf '\r\033[K[%-*s] %3d%%  %s  %s' \
          "$width" "$(printf '%*s' "$filled" '' | tr ' ' '=')" \
          "$pct" "$label" "$speed"
      fi
      last_pct="$pct"
    fi
  done
  printf '\n'
}

run_ffmpeg() {
  local label="$1"; shift
  "$@" -hide_banner -loglevel error -nostats -progress pipe:1 2>"$LOG" \
    | progress "$label"
  local status=${PIPESTATUS[0]}
  if [[ "$status" -ne 0 ]]; then
    echo "ERRORE in '$label' (exit $status). Ultime righe del log:" >&2
    tail -n 20 "$LOG" >&2
    exit "$status"
  fi
  rm -f "$LOG"
}

# ---------- Elaborazione sequenziale (default: un solo passaggio) ----------
sequential_run() {
  echo ""
  echo "==> Passaggio 1/2: rilevamento movimento (vidstabdetect)..."
  PROG_BASE=0; PROG_RANGE=20
  run_ffmpeg "analisi movimento" \
    ffmpeg -y -i "$INPUT" \
    -vf "${DETECT_FILTER}$WORKDIR/transforms.trf" \
    -f null -

  echo "==> Passaggio 2/2: stabilizzazione + miglioramento immagine..."
  if [[ "$AUDIO_NORMALIZE" == "true" ]]; then
    AUDIO_ARGS="-af loudnorm=I=-16:TP=-1.5:LRA=11 -c:a aac -b:a 160k"
  else
    AUDIO_ARGS="-c:a copy"
  fi

  if [[ -n "$CSV" ]]; then
    echo "==> Genero overlay OSD + minimappa..."
    write_progress "overlay" 0 20 ""
    python3 "$PY_SCRIPT" --video "$INPUT" --csv "$CSV" \
      --offset "$OFFSET" --render "$WORKDIR" --dur "$DURATION" \
      --progress-file "$PROGRESS_FILE" --progress-base 20 --progress-range 10
    write_progress "overlay" 100 30 ""

    FC="[0:v]${TRANSFORM_FILTER}$WORKDIR/transforms.trf${TRANSFORM_TAIL}[base];"
    FC+="[1:v]scale=${MINIMAP_TARGET}:-1,drawbox=x=0:y=0:w=iw:h=ih:color=white@0.85:t=2[m];"
    FC+="[base][m]overlay=W-w-20:H-h-20:eof_action=repeat[v0];"
    FC+="[v0][2:v]overlay=20:H-h-20:eof_action=repeat[v]"
    PROG_BASE=30; PROG_RANGE=70
    run_ffmpeg "elaborazione" \
      ffmpeg -y -i "$INPUT" -i "$WORKDIR/minimap.mp4" -i "$WORKDIR/osd.mov" \
      -filter_complex "$FC" \
      -map "[v]" -map "0:a" \
      $ENCODE_OPTS -pix_fmt yuv420p \
      -movflags +faststart -map_metadata -1 \
      $AUDIO_ARGS \
      "$OUTPUT"
  else
    PROG_BASE=30; PROG_RANGE=70
    run_ffmpeg "elaborazione" \
      ffmpeg -y -i "$INPUT" \
      -vf "${TRANSFORM_FILTER}$WORKDIR/transforms.trf${TRANSFORM_TAIL}" \
      $ENCODE_OPTS -pix_fmt yuv420p \
      -movflags +faststart -map_metadata -1 \
      $AUDIO_ARGS \
      "$OUTPUT"
  fi
}

# ---------- Elaborazione parallela (segmenti) ----------
parallel_run() {
  echo ""
  echo "==> Divido il video in segmenti di ${SEG_SECONDS}s..."

  # Split lossless (copia di stream, taglio sui keyframe)
  ffmpeg -y -hide_banner -loglevel error -i "$INPUT" \
    -map 0:v:0 -map 0:a:0 -c copy \
    -f segment -segment_time "$SEG_SECONDS" -reset_timestamps 1 \
    -avoid_negative_ts make_zero \
    "$WORKDIR/seg_%05d.mp4"

  SEGS=( "$WORKDIR"/seg_*.mp4 )
  TOTAL=${#SEGS[@]}
  if [[ "$TOTAL" -eq 0 ]]; then
    echo "ERRORE: nessun segmento generato" >&2
    exit 1
  fi
  mkdir -p "$WORKDIR/done" "$WORKDIR/logs"

  # Worker: processa un singolo segmento (detect + transform + encode)
  cat > "$WORKDIR/worker.sh" <<'WEOF'
#!/usr/bin/env bash
set -euo pipefail
seg="$1"
name="$(basename "${seg%.mp4}")"
trf="$WORKDIR/$name.trf"
out="$WORKDIR/${name}_enh.mp4"
log="$WORKDIR/logs/$name.log"

ffmpeg -y -hide_banner -loglevel error -i "$seg" \
  -vf "${DETECT_FILTER}${trf}" -f null - 2>"$log" \
  || { echo "ERRORE detect $name"; tail -n 20 "$log" >&2; exit 1; }

ffmpeg -y -hide_banner -loglevel error -i "$seg" \
  -vf "${TRANSFORM_FILTER}${trf}${TRANSFORM_TAIL}" \
  $ENCODE_OPTS -pix_fmt yuv420p \
  -c:a copy -map_metadata -1 -max_muxing_queue_size 1024 \
  "$out" 2>"$log" \
  || { echo "ERRORE encode $name"; tail -n 20 "$log" >&2; exit 1; }

rm -f "$trf" "$seg" "$log"
touch "$WORKDIR/done/$name.ok"
WEOF

  export COLOR_FILTER DETECT_FILTER TRANSFORM_FILTER TRANSFORM_TAIL \
         ENCODE_OPTS WORKDIR

  echo "==> Elaboro $TOTAL segmenti con $JOBS worker in parallelo..."

  # Monitor: mostra i segmenti completati
  monitor() {
    while :; do
      n=$(ls "$WORKDIR"/done/*.ok 2>/dev/null | wc -l | tr -d ' ')
      printf '\r\033[KElaborazione: %d/%d segmenti completati' "$n" "$TOTAL"
      [[ "$n" -ge "$TOTAL" ]] && break
      sleep 0.5
    done
    printf '\n'
  }
  monitor & MON_PID=$!

  set +e
  printf '%s\n' "${SEGS[@]}" | xargs -P "$JOBS" -n1 bash "$WORKDIR/worker.sh"
  XARGS_STATUS=$?
  set -e

  kill "$MON_PID" 2>/dev/null || true
  wait "$MON_PID" 2>/dev/null || true
  n=$(ls "$WORKDIR"/done/*.ok 2>/dev/null | wc -l | tr -d ' ')
  printf '\r\033[KElaborazione: %d/%d segmenti completati\n' "$n" "$TOTAL"

  if [[ "$XARGS_STATUS" -ne 0 || "$n" -lt "$TOTAL" ]]; then
    echo "ERRORE: elaborazione parallela fallita ($n/$TOTAL completati)." >&2
    for l in "$WORKDIR"/logs/*.log; do
      [[ -f "$l" ]] && { echo "--- $l ---" >&2; tail -n 5 "$l" >&2; }
    done
    exit 1
  fi

  # Lista concat in ordine
  : > "$WORKDIR/concat.txt"
  for s in "${SEGS[@]}"; do
    echo "file '$(basename "${s%.mp4}")_enh.mp4'" >> "$WORKDIR/concat.txt"
  done

  echo "==> Unisco i segmenti..."
  ffmpeg -y -hide_banner -loglevel error -f concat -safe 0 \
    -i "$WORKDIR/concat.txt" -c copy -movflags +faststart \
    -max_muxing_queue_size 1024 \
    "$WORKDIR/joined.mp4"

  if [[ "$AUDIO_NORMALIZE" == "true" ]]; then
    echo "==> Normalizzo l'audio (volume uniforme)..."
    ffmpeg -y -hide_banner -loglevel error -i "$WORKDIR/joined.mp4" \
      -c:v copy -af loudnorm=I=-16:TP=-1.5:LRA=11 -c:a aac -b:a 160k \
      -movflags +faststart "$OUTPUT"
  else
    mv "$WORKDIR/joined.mp4" "$OUTPUT"
  fi
}

# ---------- Rilevamento automatico dell'offset ----------
if [[ "$OFFSET" == "auto" ]]; then
  if [[ -z "$CSV" ]]; then
    echo "ERRORE: --offset auto richiede --csv" >&2
    exit 1
  fi
  echo "==> Rilevo l'offset automaticamente (analisi audio + throttle)..."
  OFFSET=$(python3 "$DETECT_SCRIPT" --video "$ORIG_INPUT" --csv "$CSV" --quiet) \
    || { echo "ERRORE: rilevamento offset fallito" >&2; exit 1; }
  echo "==> Offset rilevato: ${OFFSET}s"
fi

# ---------- Scelta modalità ----------
if [[ "$PARALLEL" == "true" ]] && \
   [[ "$(awk -v d="$DURATION" 'BEGIN{print (d > 30) ? 1 : 0}')" == "1" ]]; then
  parallel_run
else
  sequential_run
fi

# ---------- Verifica finale ----------
write_progress "completato" 100 100 ""
OUT_DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUTPUT" 2>/dev/null || echo 0)
echo "==> Fatto! Video generato: $OUTPUT"
echo "    Durata: input $(echo "$DURATION" | awk '{printf "%.1f s", $1}') -> output $(echo "$OUT_DUR" | awk '{printf "%.1f s", $1}')"
if [[ "$(awk -v a="$DURATION" -v b="$OUT_DUR" 'BEGIN{print (a-b > 0.5 || b-a > 0.5) ? 1 : 0}')" == "1" ]]; then
  echo "ATTENZIONE: durata input/output non combacia (possibile scarto ai bordi dei segmenti)." >&2
fi
