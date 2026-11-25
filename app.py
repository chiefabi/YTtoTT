"""YT -> TikTok Cutter: lokaler Server (Flask) + Browser-UI.

Start: start.bat  (oeffnet http://127.0.0.1:5055)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

import numpy as np
from flask import Flask, abort, jsonify, request, send_file, send_from_directory

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
EXPORT = ROOT / "export"
CTA = ROOT / "cta"
MODELS = ROOT / "models"
for p in (WORK, EXPORT, CTA, MODELS):
    p.mkdir(exist_ok=True)

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PID_RE = re.compile(r"^[0-9a-f]{12}$")

app = Flask(__name__, static_folder=None)
JOBS = {}
PORT = int(os.environ.get("YTTT_PORT", "5055"))
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".flv", ".ts", ".wmv"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".mp4", ".mov"}
URL_RE = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)


@app.before_request
def guard_local_only():
    """Schutz gegen fremde Webseiten, die den lokalen Server ansprechen (CSRF / DNS-Rebinding)."""
    if request.host not in ALLOWED_HOSTS:
        abort(403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin is not None:
            if origin not in ALLOWED_ORIGINS:
                abort(403)
        elif request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            abort(403)


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


# ----------------------------------------------------------------- helpers
def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=NOWIN, **kw)


def which(name):
    p = shutil.which(name)
    if not p:
        raise RuntimeError(f"{name} nicht gefunden (nicht im PATH)")
    return p


def new_job():
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"status": "running", "progress": 0.0, "message": "", "result": None, "error": None}
    return jid


def start_job(fn, *args):
    jid = new_job()
    job = JOBS[jid]

    def runner():
        try:
            job["result"] = fn(job, *args)
            job["status"] = "done"
            job["progress"] = 1.0
        except Exception as e:  # noqa
            job["status"] = "error"
            job["error"] = str(e)

    threading.Thread(target=runner, daemon=True).start()
    return jid


def pdir(pid):
    if not PID_RE.match(pid or ""):
        abort(404)
    d = WORK / pid
    if not d.exists():
        abort(404)
    return d


def probe(path):
    r = run([which("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    j = json.loads(r.stdout)
    v = next(s for s in j["streams"] if s["codec_type"] == "video")
    num, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    dur = float(j["format"].get("duration") or v.get("duration") or 0)
    return {"width": int(v["width"]), "height": int(v["height"]), "fps": fps, "duration": dur,
            "vcodec": v["codec_name"], "has_audio": any(s["codec_type"] == "audio" for s in j["streams"])}


def slug(s):
    return re.sub(r"[^\w\-]+", "_", s, flags=re.UNICODE).strip("_")[:50] or "video"


# --------------------------------------------------------- project creation
def compute_peaks(src, info, job, lo, hi):
    """Wellenform: 100 Werte pro Sekunde (0-255)."""
    if not info["has_audio"]:
        return []
    p = subprocess.Popen([which("ffmpeg"), "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "2000",
                          "-f", "s16le", "-"], stdout=subprocess.PIPE, creationflags=NOWIN)
    expected = max(info["duration"] * 2000 * 2, 1)
    buf = bytearray()
    while True:
        chunk = p.stdout.read(1 << 20)
        if not chunk:
            break
        buf += chunk
        f = min(1.0, len(buf) / expected)
        job["progress"] = lo + f * (hi - lo)
        job["message"] = f"Berechne Wellenform ... {int(f * 100)}%"
    p.wait()
    data = np.frombuffer(bytes(buf), dtype="<i2")
    n = len(data) // 20
    if not n:
        return []
    pk = np.abs(data[: n * 20].astype(np.int32).reshape(n, 20)).max(axis=1).astype(np.float32)
    ref = max(float(np.percentile(pk, 99.5)), 1.0)
    return np.clip(pk / ref * 255, 0, 255).astype(int).tolist()


def postprocess(job, pid, title, src):
    d = WORK / pid
    job["message"] = "Analysiere Video ..."
    job["progress"] = 0.02
    info = probe(src)
    preview = src.name
    wave_lo = 0.05
    if info["vcodec"] != "h264" or src.suffix.lower() not in (".mp4", ".mov", ".m4v"):
        out = d / "preview.mp4"
        p = subprocess.Popen([which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1",
                              "-nostats", "-i", str(src), "-vf", "scale='min(1280,iw)':-2", "-c:v", "libx264",
                              "-preset", "veryfast", "-crf", "26", "-c:a", "aac", "-b:a", "128k", str(out)],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, creationflags=NOWIN)
        for line in p.stdout:
            if line.startswith("out_time_us="):
                try:
                    f = min(1.0, int(line.split("=")[1]) / 1e6 / max(info["duration"], 1))
                    job["progress"] = 0.05 + f * 0.6
                    job["message"] = f"Erzeuge Vorschau-Datei ... {int(f * 100)}%"
                except ValueError:
                    pass
        p.wait()
        if p.returncode:
            raise RuntimeError("Vorschau-Konvertierung fehlgeschlagen")
        preview = out.name
        wave_lo = 0.65
    peaks = compute_peaks(src, info, job, wave_lo, 0.98)
    job["message"] = "Speichere Projekt ..."
    meta = {"id": pid, "title": title, "source": src.name, "preview": preview, "peaks_per_sec": 100,
            "created": time.time(), **info}
    (d / "project.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / "peaks.json").write_text(json.dumps(peaks), encoding="utf-8")
    return {"pid": pid}


def repeaks_job(job, pid):
    d = WORK / pid
    meta = json.loads((d / "project.json").read_text(encoding="utf-8"))
    peaks = compute_peaks(d / meta["source"], meta, job, 0.0, 0.98)
    (d / "peaks.json").write_text(json.dumps(peaks), encoding="utf-8")
    meta["peaks_per_sec"] = 100
    (d / "project.json").write_text(json.dumps(meta), encoding="utf-8")
    return {"pid": pid}


@app.route("/api/repeaks/<pid>", methods=["POST"])
def api_repeaks(pid):
    pdir(pid)
    return jsonify(job=start_job(repeaks_job, pid))


def download_job(job, pid, url):
    d = WORK / pid
    cmd = [which("yt-dlp"), "-f",
           "bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]/bv*[height<=1080]+ba/b",
           "--merge-output-format", "mp4", "--no-playlist", "--write-info-json", "--newline",
           "--js-runtimes", "node", "--remote-components", "ejs:github",
           "-o", str(d / "source.%(ext)s"), "--", url]
    job["message"] = "Lade von YouTube ..."
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="replace", creationflags=NOWIN)
    tail = []
    phase, last = 0, 0.0
    base, wt = [0.0, 0.84, 0.88], [0.84, 0.04, 0.02]
    for line in p.stdout:
        tail = (tail + [line.strip()])[-12:]
        m = re.search(r"\[download\]\s+([\d.]+)%", line)
        if m:
            pc = float(m.group(1))
            if pc < last - 20:
                phase = min(phase + 1, 2)
            last = pc
            job["progress"] = base[phase] + pc / 100 * wt[phase]
            job["message"] = f"Lade von YouTube ... {int(job['progress'] * 100)}%"
    p.wait()
    src = d / "source.mp4"
    if p.returncode or not src.exists():
        raise RuntimeError("yt-dlp Fehler:\n" + "\n".join(tail))
    title = "video"
    ij = d / "source.info.json"
    if ij.exists():
        title = json.loads(ij.read_text(encoding="utf-8")).get("title", title)
    return postprocess(job, pid, title, src)


def upload_post_job(job, pid, src_name, title):
    return postprocess(job, pid, title, WORK / pid / src_name)


# ----------------------------------------------------------------- routes
@app.route("/")
def index():
    return send_file(ROOT / "index.html")


@app.route("/api/projects")
def projects():
    out = []
    for d in sorted(WORK.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        pj = d / "project.json"
        if pj.exists():
            m = json.loads(pj.read_text(encoding="utf-8"))
            out.append({"id": m["id"], "title": m["title"], "duration": m["duration"]})
    return jsonify(out)


@app.route("/api/project/<pid>")
def project(pid):
    d = pdir(pid)
    meta = json.loads((d / "project.json").read_text(encoding="utf-8"))
    state = None
    sj = d / "state.json"
    if sj.exists():
        state = json.loads(sj.read_text(encoding="utf-8"))
    peaks = json.loads((d / "peaks.json").read_text(encoding="utf-8")) if (d / "peaks.json").exists() else []
    return jsonify({"meta": meta, "state": state, "peaks": peaks})


@app.route("/api/save/<pid>", methods=["POST"])
def save(pid):
    d = pdir(pid)
    (d / "state.json").write_text(json.dumps(request.get_json(force=True)), encoding="utf-8")
    return jsonify(ok=True)


@app.route("/api/delete/<pid>", methods=["POST"])
def delete_project(pid):
    shutil.rmtree(pdir(pid), ignore_errors=True)
    return jsonify(ok=True)


@app.route("/media/<pid>/<name>")
def media(pid, name):
    return send_from_directory(pdir(pid), name, conditional=True)


@app.route("/api/download", methods=["POST"])
def api_download():
    url = (request.get_json(force=True).get("url") or "").strip()
    if not url:
        return jsonify(error="Keine URL"), 400
    if not URL_RE.match(url):
        return jsonify(error="Bitte einen vollstaendigen Link mit http:// oder https:// eingeben."), 400
    pid = uuid.uuid4().hex[:12]
    (WORK / pid).mkdir()
    return jsonify(job=start_job(download_job, pid, url))


@app.route("/api/upload", methods=["POST"])
def api_upload():
    name = Path(request.args.get("name", "video.mp4")).name
    ext = Path(name).suffix.lower() or ".mp4"
    if ext not in VIDEO_EXT:
        return jsonify(error="Dateityp nicht erlaubt"), 400
    pid = uuid.uuid4().hex[:12]
    d = WORK / pid
    d.mkdir()
    src = d / f"source{ext}"
    with open(src, "wb") as f:
        shutil.copyfileobj(request.stream, f, 1024 * 1024)
    return jsonify(job=start_job(upload_post_job, pid, src.name, Path(name).stem))


@app.route("/api/job/<jid>")
def api_job(jid):
    j = JOBS.get(jid)
    if not j:
        abort(404)
    return jsonify(j)


# -------- CTA audio
@app.route("/api/cta")
def cta_list():
    files = []
    for f in sorted(CTA.iterdir()):
        if f.suffix.lower() in (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".mp4", ".mov"):
            try:
                dur = probe_audio_duration(f)
            except Exception:
                dur = 0
            files.append({"name": f.name, "duration": dur})
    return jsonify(files)


def probe_audio_duration(f):
    r = run([which("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(f)])
    return float(r.stdout.strip())


@app.route("/api/cta/upload", methods=["POST"])
def cta_upload():
    name = Path(request.args.get("name", "cta.mp3")).name
    if Path(name).suffix.lower() not in AUDIO_EXT or ":" in name:
        return jsonify(error="Dateityp nicht erlaubt"), 400
    with open(CTA / name, "wb") as f:
        shutil.copyfileobj(request.stream, f, 1024 * 1024)
    return jsonify(ok=True, name=name)


@app.route("/cta/<name>")
def cta_file(name):
    return send_from_directory(CTA, name, conditional=True)


# ----------------------------------------------------------- transcription
_models = {}


def get_model(name):
    if name not in _models:
        from faster_whisper import WhisperModel
        _models[name] = WhisperModel(name, device="cpu", compute_type="int8",
                                     cpu_threads=min(16, os.cpu_count() or 4), download_root=str(MODELS))
    return _models[name]


def seg_filter_audio(segs):
    parts = [f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]" for i, (s, e) in enumerate(segs)]
    ins = "".join(f"[a{i}]" for i in range(len(segs)))
    return ";".join(parts) + f";{ins}concat=n={len(segs)}:v=0:a=1[a]"


def transcribe_job(job, pid, st):
    d = WORK / pid
    meta = json.loads((d / "project.json").read_text(encoding="utf-8"))
    segs = [(float(sg[0]), float(sg[1])) for sg in st["segs"] if float(sg[1]) - float(sg[0]) > 0.05 and not (len(sg) > 3 and sg[3])]
    if not segs:
        raise RuntimeError("Keine Videoabschnitte vorhanden")
    job["message"] = "Bereite Audio vor ..."
    (d / "a_filter.txt").write_text(seg_filter_audio(segs), encoding="utf-8")
    wav = d / "speech.wav"
    r = run([which("ffmpeg"), "-y", "-i", str(d / meta["source"]), "-/filter_complex", "a_filter.txt",
             "-map", "[a]", "-ar", "16000", "-ac", "1", str(wav)], cwd=str(d))
    if r.returncode:
        raise RuntimeError("Audio-Extraktion fehlgeschlagen:\n" + r.stderr[-500:])
    table, off = [], 0.0
    for s, e in segs:
        table.append((off, s, e - s))
        off += e - s
    total = off

    def to_src(t):
        for o, s, ln in table:
            if o <= t < o + ln + 1e-6:
                return s + (t - o)
        return table[-1][1] + table[-1][2]

    job["message"] = "Lade Whisper-Modell (beim ersten Mal dauert das) ..."
    model = get_model(st.get("model") or "large-v3-turbo")
    lang = st.get("language") or None
    if lang == "auto":
        lang = None
    job["message"] = "Erkenne Sprache ..."
    import wave
    with wave.open(str(wav), "rb") as wf:
        audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    it, info = model.transcribe(audio, language=lang, word_timestamps=True, vad_filter=True,
                                initial_prompt=(st.get("prompt") or None), beam_size=3,
                                condition_on_previous_text=False)
    words = []
    for seg in it:
        job["progress"] = min(0.98, seg.end / max(total, 0.1))
        job["message"] = f"Transkribiere ... {int(job['progress'] * 100)}%"
        for w in seg.words or []:
            txt = w.word.strip()
            if not txt:
                continue
            s0 = to_src(w.start)
            words.append({"s": s0, "e": s0 + max(0.05, w.end - w.start), "t": txt})

    maxw = int(st.get("words") or 3)
    cues, cur = [], []

    def flush():
        if cur:
            cues.append({"s": cur[0]["s"], "e": cur[-1]["e"], "t": " ".join(w["t"] for w in cur)})
            cur.clear()

    for w in words:
        if cur:
            gap = w["s"] - cur[-1]["e"]
            chars = sum(len(x["t"]) + 1 for x in cur) + len(w["t"])
            if len(cur) >= maxw or gap > 0.6 or chars > 26:
                flush()
        cur.append(w)
        if re.search(r"[.!?…]$", w["t"]) and len(cur) >= 2:
            flush()
    flush()
    for i, c in enumerate(cues):
        nxt = cues[i + 1]["s"] if i + 1 < len(cues) else c["e"] + 1
        c["e"] = min(c["e"] + 0.2, max(nxt, c["e"]))
    return {"cues": cues, "language": info.language}


@app.route("/api/transcribe/<pid>", methods=["POST"])
def api_transcribe(pid):
    pdir(pid)
    return jsonify(job=start_job(transcribe_job, pid, request.get_json(force=True)))


# ------------------------------------------------------------------ render
def ass_time(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_color(hexcol):
    h = (hexcol or "#ffffff").lstrip("#")
    if len(h) != 6:
        h = "ffffff"
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper().replace("&H", "&H")


def build_ass(cues, segs, sub):
    y = int(float(sub.get("y", 0.68)) * 1920)
    size = int(sub.get("size", 88))
    font = sub.get("font") or "Arial Black"
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Default,{font},{size},{ass_color(sub.get('color'))},&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,"
        f"{max(2, size // 12)},2,5,90,90,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    n = 0
    off = 0.0
    spans = []
    for s, e in segs:
        spans.append((off, s, e))
        off += e - s
    for c in sorted(cues, key=lambda c: c["s"]):
        text = (c.get("t") or "").strip()
        if not text:
            continue
        if sub.get("upper"):
            text = text.upper()
        text = text.replace("{", "(").replace("}", ")").replace("\r", "").replace("\n", "\\N")
        for o, s, e in spans:
            a, b = max(c["s"], s), min(c["e"], e)
            if b - a > 0.05:
                lines.append(f"Dialogue: 0,{ass_time(o + a - s)},{ass_time(o + b - s)},Default,,0,0,0,,"
                             f"{{\\an5\\pos(540,{y})}}{text}")
                n += 1
    return "\n".join(lines) + "\n", n


def crop_expr(r, W, H):
    w = max(2, int(r["w"] * W) // 2 * 2)
    h = max(2, int(r["h"] * H) // 2 * 2)
    x = min(max(0, int(r["x"] * W) // 2 * 2), W - w)
    y = min(max(0, int(r["y"] * H) // 2 * 2), H - h)
    return f"crop={w}:{h}:{x}:{y}"


DEFAULT_LAYOUT = {"mode": "game", "game": {"x": 0.3, "y": 0.0, "w": 0.4, "h": 1.0}}


def layout_chain(i, L, W, H):
    """Filterkette fuer einen Clip: [t{i}] -> [v{i}] (1080x1920)."""
    t, out = f"[t{i}]", f"[v{i}]"
    mode = L.get("mode", "game")
    camH = int(round(float(L.get("camH", 0.34)) * 1920 / 2) * 2)
    game = L.get("game") or DEFAULT_LAYOUT["game"]
    cam = L.get("cam")
    gfit = L.get("gameFit") or {"x": 0, "y": 0, "w": 1, "h": 1}
    fin = ",format=yuv420p"
    sc = ":flags=lanczos,setsar=1"
    if mode in ("stack", "overlay", "fitcam") and not cam:
        mode = "fit" if mode == "fitcam" else "game"
    if mode == "game":
        return [f"{t}{crop_expr(game, W, H)},scale=1080:1920{sc}{fin}{out}"]
    if mode in ("stack", "overlay"):
        gh = 1920 - camH if mode == "stack" else 1920
        r = [f"{t}split=2[ga{i}][ca{i}]",
             f"[ga{i}]{crop_expr(game, W, H)},scale=1080:{gh}{sc}[g{i}]",
             f"[ca{i}]{crop_expr(cam, W, H)},scale=1080:{camH}{sc}[c{i}]"]
        r.append(f"[c{i}][g{i}]vstack=inputs=2{fin}{out}" if mode == "stack"
                 else f"[g{i}][c{i}]overlay=0:0{fin}{out}")
        return r
    # fit / fitcam: ganzes Bild (beliebiges Seitenverhaeltnis) mit Balken oder Blur
    withcam = mode == "fitcam"
    blur = L.get("bg") == "blur"
    a = (gfit["w"] * W) / max(gfit["h"] * H, 1)
    avail = 1920 - (camH if withcam else 0)
    gw, gh = 1080, int(1080 / a) // 2 * 2
    if gh > avail:
        gh = avail // 2 * 2
        gw = int(gh * a) // 2 * 2
    gw, gh = max(gw, 2), max(gh, 2)
    x = (1080 - gw) // 2 // 2 * 2
    y = camH if withcam else int((1920 - gh) * float(L.get("fitY", 0.5))) // 2 * 2
    names = [f"ga{i}"] + ([f"ca{i}"] if withcam else []) + ([f"ba{i}"] if blur else [])
    if len(names) > 1:
        r = [f"{t}split={len(names)}" + "".join(f"[{n}]" for n in names)]
    else:
        r = [f"{t}null[{names[0]}]"]
    r.append(f"[ga{i}]{crop_expr(gfit, W, H)},scale={gw}:{gh}{sc}[g{i}]")
    if withcam:
        r.append(f"[ca{i}]{crop_expr(cam, W, H)},scale=1080:{camH}{sc}[c{i}]")
    if blur:
        r.append(f"[ba{i}]{crop_expr(gfit, W, H)},scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
                 f"boxblur=8:2,scale=1080:1920,eq=brightness=-0.1,setsar=1[b{i}]")
        base = f"[b{i}]"
        if withcam:
            r.append(f"{base}[c{i}]overlay=0:0[o{i}]")
            base = f"[o{i}]"
        r.append(f"{base}[g{i}]overlay={x}:{y}{fin}{out}")
    elif withcam:
        r.append(f"[g{i}]pad=1080:{gh}:{x}:0:black[p{i}]")
        r.append(f"[c{i}][p{i}]vstack=inputs=2,pad=1080:1920:0:0:black{fin}{out}")
    else:
        r.append(f"[g{i}]pad=1080:1920:{x}:{y}:black{fin}{out}")
    return r


def render_job(job, pid, st):
    d = WORK / pid
    meta = json.loads((d / "project.json").read_text(encoding="utf-8"))
    W, H = meta["width"], meta["height"]
    dur = meta["duration"]
    segs, lays = [], []
    for sg in st["segs"]:
        if len(sg) > 3 and sg[3]:
            continue  # ausgeschalteter Clip
        s0, e0 = max(0.0, float(sg[0])), min(dur, float(sg[1]))
        if e0 - s0 > 0.05:
            segs.append((s0, e0))
            lays.append(sg[2] if len(sg) > 2 and sg[2] else (st.get("layout") or DEFAULT_LAYOUT))
    hk = st.get("hook") or {}
    if hk.get("on"):
        hs = max(0.0, float(hk.get("s", 0)))
        he = min(dur, float(hk.get("e", hs + 5)), hs + 8.0)
        if he - hs > 0.1:
            best, bd = None, 1e18   # Layout des Clips an der Hook-Stelle (auch ausgeschaltete Clips zaehlen)
            for sg in st["segs"]:
                d0 = 0.0 if float(sg[0]) <= hs < float(sg[1]) else min(abs(hs - float(sg[0])), abs(hs - float(sg[1])))
                if d0 < bd and len(sg) > 2 and sg[2]:
                    best, bd = sg[2], d0
            segs.insert(0, (hs, he))
            lays.insert(0, best or (lays[0] if lays else DEFAULT_LAYOUT))
    if not segs:
        raise RuntimeError("Keine Videoabschnitte vorhanden")
    T = sum(e - s for s, e in segs)

    f = []
    for i, ((s0, e0), L) in enumerate(zip(segs, lays)):
        f.append(f"[0:v]trim=start={s0:.3f}:end={e0:.3f},setpts=PTS-STARTPTS[t{i}]")
        f.extend(layout_chain(i, L, W, H))
        f.append(f"[0:a]atrim=start={s0:.3f}:end={e0:.3f},asetpts=PTS-STARTPTS[a{i}]")
    ins = "".join(f"[v{i}][a{i}]" for i in range(len(segs)))
    f.append(f"{ins}concat=n={len(segs)}:v=1:a=1[lay][ac]")

    sub = st.get("sub", {})
    n_ev = 0
    if sub.get("on", True) and st.get("cues"):
        ass, n_ev = build_ass(st["cues"], segs, sub)
        (d / "subs.ass").write_text(ass, encoding="utf-8-sig")
    f.append("[lay]ass=subs.ass[vout]" if n_ev else "[lay]null[vout]")

    cta = st.get("cta") or {}
    cta_path = None
    if cta.get("file") and (CTA / Path(cta["file"]).name).exists():
        cta_path = CTA / Path(cta["file"]).name
        cd = probe_audio_duration(cta_path)
        start = max(0.0, T - cd) if cta.get("mode", "end") == "end" else float(cta.get("at", 0))
        ms = int(start * 1000)
        vol = float(cta.get("vol", 100)) / 100
        duck = float(cta.get("duck", 100)) / 100
        a_main = "[ac]"
        if duck < 0.99:
            f.append(f"[ac]volume={duck:.2f}:enable='between(t,{start:.3f},{start + cd:.3f})'[ad]")
            a_main = "[ad]"
        f.append(f"[1:a]volume={vol:.2f},adelay={ms}:all=1[cta]")
        f.append(f"{a_main}[cta]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
    else:
        f.append("[ac]anull[aout]")
    (d / "filter.txt").write_text(";\n".join(f), encoding="utf-8")

    name = slug(st.get("outname") or meta["title"])
    out = EXPORT / f"{name}_tiktok.mp4"
    k = 2
    while out.exists():
        out = EXPORT / f"{name}_tiktok_{k}.mp4"
        k += 1
    fps = min(60.0, meta.get("fps") or 30.0)
    cmd = [which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
           "-i", str(d / meta["source"])]
    if cta_path:
        cmd += ["-i", str(cta_path)]
    cmd += ["-/filter_complex", "filter.txt", "-map", "[vout]", "-map", "[aout]",
            "-r", f"{fps:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(st.get("crf", 18))),
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    job["message"] = "Rendere ..."
    log = open(d / "render.log", "w", encoding="utf-8")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, text=True, cwd=str(d), creationflags=NOWIN)
    for line in p.stdout:
        if line.startswith("out_time_us="):
            try:
                job["progress"] = min(0.99, int(line.split("=")[1]) / 1e6 / T)
                job["message"] = f"Rendere ... {int(job['progress'] * 100)}%"
            except ValueError:
                pass
    p.wait()
    log.close()
    if p.returncode:
        raise RuntimeError("ffmpeg Fehler:\n" + (d / "render.log").read_text(encoding="utf-8", errors="replace")[-800:])
    return {"file": out.name, "duration": T}


@app.route("/api/render/<pid>", methods=["POST"])
def api_render(pid):
    pdir(pid)
    return jsonify(job=start_job(render_job, pid, request.get_json(force=True)))


@app.route("/export/<name>")
def export_file(name):
    return send_from_directory(EXPORT, name, conditional=True)


@app.route("/api/open-export", methods=["POST"])
def open_export():
    os.startfile(str(EXPORT))
    return jsonify(ok=True)


if __name__ == "__main__":
    port = PORT
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host="127.0.0.1", port=port, threaded=True)
