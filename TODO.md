# TODO — VideoFlight

Backlog di miglioramenti futuri (riflessi anche come issue su GitHub).

## Da fare

- [ ] **Toggle unità OSD** (km/h ↔ nodi) — opzione configurabile per la velocità.
- [ ] **Minimappa adattiva** — viewport che segue la posizione (o tracciato opzionale).
- [ ] **Drag&drop** dei file video/CSV nella GUI.
- [ ] **Doppia vista input/output** nel player (confronto prima/dopo).
- [ ] **Batch** — coda di più video da processare in sequenza.
- [ ] **Loudness report** — valori LUFS nel registro (primo/secondo passaggio loudnorm).
- [ ] **Pulizia cache tile** (`~/.cache/osm_tiles`) con soglia di dimensione massima.

## Fatto

- [x] Stabilizzazione + miglioramento colore/audio (un solo passaggio sequenziale)
- [x] Overlay OSD + minimappa (posizione fluida con spline)
- [x] GUI Tkinter (picker, anteprima, conversione, annulla)
- [x] Barre di avanzamento come controlli GUI (step + fase + velocità/ETA)
- [x] Player video integrato (play/pausa/riavvolgi/slider/loop)
- [x] Kill del gruppo di processi (niente figli orfani)
- [x] Auto-rilevamento offset (audio motore ↔ throttle CSV)
- [x] Impostazioni persistenti (JSON)
- [x] Progress live della fase overlay
- [x] Batteria (BAT) nell'OSD
- [x] Stima dimensione output
