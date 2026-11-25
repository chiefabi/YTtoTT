# YT → TikTok Cutter

Lokales Werkzeug (Windows), um YouTube-Videos schnell in 9:16-TikTok-Videos (1080×1920) zu schneiden:
Clips schneiden/ausschalten/verbinden, pro Clip ein eigenes Layout (Gameplay, Webcam oben, ganzes Bild mit
Balken oder Blur), Hook (max. 8 s) vor dem Video, automatische Untertitel (Whisper, lokal) mit Korrektur,
CTA-Audiospur und Export per ffmpeg. Alles läuft lokal im Browser auf `127.0.0.1:5055`.

## Voraussetzungen
- Python 3.10+ (`pip install -r requirements.txt`)
- [ffmpeg](https://ffmpeg.org) (ab 7.x/8.x, im `PATH`) und [yt-dlp](https://github.com/yt-dlp/yt-dlp) (im `PATH`)
- [Node.js](https://nodejs.org) (yt-dlp braucht es für YouTube)

## Start
`start.bat` ausführen. Der Browser öffnet `http://127.0.0.1:5055`. Das Whisper-Modell wird beim ersten Mal
automatisch nach `models/` geladen (~1,6 GB).

## Sicherheit
- Der Server lauscht nur auf `127.0.0.1` und lehnt fremde `Host`-/`Origin`-Header ab (Schutz vor CSRF / DNS-Rebinding).
- Es gibt keine Anmeldung: Nicht ins Netzwerk oder Internet freigeben (kein Port-Forwarding, kein `host=0.0.0.0`).
- Nutzerdaten (`work/`, `export/`, `cta/`, `models/`) sind per `.gitignore` ausgeschlossen.

## Hinweis
Lade nur Videos herunter und bearbeite sie, wenn du die Rechte daran hast bzw. die Nutzungsbedingungen der
Plattform es erlauben. Lizenz: MIT.
