"""Local YouTube downloader with a web UI.

Run:  python app.py      (opens http://127.0.0.1:5000 in your browser)
"""
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

import imageio_ffmpeg
import yt_dlp
from flask import Flask, abort, jsonify, request

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DIR = Path(os.environ.get("YTD_DIR") or Path.home() / "Downloads")
PORT = int(os.environ.get("YTD_PORT", "5000"))
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()  # bundled ffmpeg, used to merge video + audio

YDL_BASE = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "ffmpeg_location": FFMPEG,
    # YouTube needs a JavaScript runtime for most formats; yt-dlp uses whichever is installed.
    "js_runtimes": {"deno": {}, "node": {}, "bun": {}},
}

app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="")
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

ANSI = re.compile(r"\x1b\[[0-9;]*m")
CODECS = {"avc1": "H.264", "vp09": "VP9", "vp9": "VP9", "av01": "AV1"}


# --------------------------------------------------------------------------- helpers

def clean_error(exc: Exception) -> str:
    msg = ANSI.sub("", str(exc)).replace("ERROR: ", "").strip()
    msg = re.sub(r"^\[[^\]]+\]\s*[\w-]+:\s*", "", msg)  # drop "[youtube] abc123: "
    if "is not a valid URL" in msg or "Unsupported URL" in msg:
        return "That doesn't look like a video link. Paste a YouTube URL."
    return msg.splitlines()[0] if msg else "Something went wrong."


def fsize(f: dict | None) -> int:
    if not f:
        return 0
    return int(f.get("filesize") or f.get("filesize_approx") or 0)


def est_size(f: dict, duration: float | None) -> int:
    size = fsize(f)
    if not size and f.get("tbr") and duration:
        size = int(f["tbr"] * 1000 / 8 * duration)
    return size


def quality_of(f: dict) -> int:
    """YouTube's own label (e.g. "1080p60" -> 1080), else the short side of the frame."""
    m = re.match(r"(\d{3,4})p", f.get("format_note") or "")
    if m:
        return int(m.group(1))
    h, w = f.get("height") or 0, f.get("width") or 0
    return min(h, w) if w else h


def badge_for(q: int) -> str:
    if q >= 4320:
        return "8K"
    if q >= 2160:
        return "4K"
    if q >= 1440:
        return "2K"
    if q >= 720:
        return "HD"
    return "SD"


def has_audio(f: dict) -> bool:
    return f.get("acodec") not in (None, "none")


def has_video(f: dict) -> bool:
    return f.get("vcodec") not in (None, "none")


def build_options(formats: list[dict], duration: float | None) -> list[dict]:
    audios = [f for f in formats if has_audio(f) and not has_video(f)]
    m4a = [f for f in audios if f.get("ext") == "m4a"] or audios
    best_audio = max(m4a, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)
    audio_size = est_size(best_audio, duration) if best_audio else 0

    groups: dict[int, list[dict]] = {}
    for f in formats:
        if has_video(f) and f.get("height"):
            groups.setdefault(quality_of(f), []).append(f)

    options = []
    for q, fs in groups.items():
        # Prefer H.264 (plays everywhere), then higher fps, a known size, a separate (better) video
        # stream over YouTube's low-bitrate combined file, and finally higher bitrate.
        avc = [f for f in fs if (f.get("vcodec") or "").startswith("avc1")]
        pick = max(avc or fs, key=lambda f: (f.get("fps") or 0, bool(fsize(f)), not has_audio(f), f.get("tbr") or 0))
        muxed = has_audio(pick)
        size = est_size(pick, duration) + (0 if muxed else audio_size)
        codec = CODECS.get((pick.get("vcodec") or "").split(".")[0], (pick.get("vcodec") or "").upper())
        options.append({
            "kind": "video",
            "quality": q,
            "label": f"{q}p",
            "badge": badge_for(q),
            "fps": int(max((f.get("fps") or 0) for f in fs)) or None,
            "codec": codec,
            "size": size or None,
            "height": pick.get("height"),
            "format_id": pick.get("format_id"),
            "has_audio": muxed,
        })
    options.sort(key=lambda o: o["quality"], reverse=True)

    if best_audio:
        options.append({
            "kind": "audio",
            "label": "Audio only",
            "badge": "MP3",
            "codec": "MP3 192 kbps",
            "size": int(192_000 / 8 * duration) if duration else None,
        })
    return options


def public(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}


# --------------------------------------------------------------------------- security
# The server only listens on 127.0.0.1; also refuse requests that come from other websites.

@app.before_request
def same_origin_only():
    allowed = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
    if request.host not in allowed:
        abort(403)
    origin = request.headers.get("Origin")
    if origin and origin.split("://", 1)[-1] not in allowed:
        abort(403)


# --------------------------------------------------------------------------- routes

@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/api/config")
def api_config():
    return jsonify(folder=str(DEFAULT_DIR))


@app.post("/api/info")
def api_info():
    url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    if not url:
        return jsonify(error="Paste a video link first."), 400
    try:
        with yt_dlp.YoutubeDL({**YDL_BASE, "extract_flat": "in_playlist"}) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as exc:  # yt-dlp raises many error types
        return jsonify(error=clean_error(exc)), 400

    if data.get("_type") in ("playlist", "multi_video") or not data.get("formats"):
        return jsonify(error="That's a playlist or channel. Paste a link to a single video."), 400
    if data.get("is_live"):
        return jsonify(error="This video is live right now. Try again after the stream ends."), 400

    duration = data.get("duration")
    return jsonify(
        url=data.get("webpage_url") or url,
        id=data.get("id"),
        title=data.get("title") or "Untitled",
        channel=data.get("channel") or data.get("uploader"),
        duration=duration,
        views=data.get("view_count"),
        thumbnail=data.get("thumbnail"),
        options=build_options(data["formats"], duration),
    )


@app.post("/api/download")
def api_download():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    opt = body.get("option") or {}
    if not url or opt.get("kind") not in ("video", "audio"):
        return jsonify(error="Pick a quality first."), 400

    folder = Path(body.get("folder") or DEFAULT_DIR).expanduser()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return jsonify(error=f"Can't save to “{folder}”. Choose another folder."), 400

    job = {
        "id": uuid.uuid4().hex[:12],
        "title": body.get("title") or url,
        "thumbnail": body.get("thumbnail"),
        "label": opt.get("label") if opt["kind"] == "video" else "MP3",
        "status": "queued",
        "stage": "Starting…",
        "percent": 0.0,
        "speed": None,
        "eta": None,
        "filepath": None,
        "filename": None,
        "filesize": None,
        "error": None,
        "created": time.time(),
        "_cancel": False,
    }
    with jobs_lock:
        jobs[job["id"]] = job
    threading.Thread(target=run_job, args=(job, url, opt, folder), daemon=True).start()
    return jsonify(public(job))


@app.get("/api/jobs")
def api_jobs():
    with jobs_lock:
        items = sorted(jobs.values(), key=lambda j: j["created"], reverse=True)
        return jsonify([public(j) for j in items])


def get_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        abort(404)
    return job


@app.post("/api/jobs/<job_id>/cancel")
def api_cancel(job_id):
    get_job(job_id)["_cancel"] = True
    return jsonify(ok=True)


@app.delete("/api/jobs/<job_id>")
def api_remove(job_id):
    job = get_job(job_id)
    if job["status"] in ("queued", "downloading", "processing"):
        return jsonify(error="Cancel the download first."), 400
    with jobs_lock:
        jobs.pop(job_id, None)
    return jsonify(ok=True)


@app.post("/api/jobs/<job_id>/open")
def api_open(job_id):
    path = get_job(job_id).get("filepath")
    if not path or not os.path.exists(path):
        return jsonify(error="File not found — it may have been moved or deleted."), 404
    open_path(path)
    return jsonify(ok=True)


@app.post("/api/jobs/<job_id>/reveal")
def api_reveal(job_id):
    path = get_job(job_id).get("filepath")
    if not path or not os.path.exists(path):
        return jsonify(error="File not found — it may have been moved or deleted."), 404
    if sys.platform == "win32":
        subprocess.Popen(f'explorer /select,"{path}"')
    else:
        open_path(os.path.dirname(path))
    return jsonify(ok=True)


@app.post("/api/choose-folder")
def api_choose_folder():
    """Open a native folder picker on this computer (the server runs locally)."""
    start = (request.get_json(silent=True) or {}).get("folder") or str(DEFAULT_DIR)
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(initialdir=start, title="Choose where to save videos", parent=root)
        root.destroy()
    except Exception:
        return jsonify(error="Couldn't open the folder picker. Type the folder path instead."), 500
    return jsonify(folder=os.path.normpath(chosen) if chosen else None)


def open_path(path: str):
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - opening the user's own file
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# --------------------------------------------------------------------------- download worker

def run_job(job: dict, url: str, opt: dict, folder: Path):
    parts_seen: dict[str, list[int]] = {}  # part filename -> [downloaded, total]

    def on_progress(d):
        if job["_cancel"]:
            raise yt_dlp.utils.DownloadCancelled("Cancelled by user")
        info = d.get("info_dict") or {}
        name = d.get("filename") or ""
        parts = info.get("requested_formats") or [info]
        n = len(parts)

        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            parts_seen[name] = [got, total]

            expected = [fsize(p) for p in parts]
            done = sum(v[0] for v in parts_seen.values())
            speed = d.get("speed")
            if all(expected):
                grand = max(sum(expected), sum(v[1] for v in parts_seen.values()))
                pct = done / grand * 100
                eta = (grand - done) / speed if speed else d.get("eta")
            else:
                frac = got / total if total else 0
                pct = (min(len(parts_seen), n) - 1 + frac) / n * 100
                eta = d.get("eta")

            if n > 1:
                stage = "Downloading audio…" if not has_video(info) else "Downloading video…"
            else:
                stage = "Downloading audio…" if opt["kind"] == "audio" else "Downloading…"
            job.update(status="downloading", stage=stage, percent=round(min(pct, 99.5), 1),
                       speed=speed, eta=int(eta) if eta else None)

        elif d["status"] == "finished":
            size = d.get("total_bytes") or d.get("downloaded_bytes") or 0
            parts_seen[name] = [size, size]

    def on_postprocess(d):
        if d["status"] != "started":
            return
        pp = d.get("postprocessor")
        if pp == "Merger":
            stage = "Merging video and audio…"
        elif pp == "ExtractAudio":
            stage = "Converting to MP3…"
        else:
            stage = "Finishing up…"
        job.update(status="processing", stage=stage, percent=100.0, speed=None, eta=None)

    def on_done(path):
        job["filepath"] = os.path.abspath(path)

    if opt["kind"] == "audio":
        fmt = "bestaudio[ext=m4a]/bestaudio/best"
        outtmpl = "%(title)s.%(ext)s"
        postprocessors = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    else:
        height = int(opt.get("height") or 0)
        fid = re.sub(r"[^\w-]", "", str(opt.get("format_id") or ""))
        label = re.sub(r"[^\w]", "", str(opt.get("label") or f"{height}p"))
        fallback = f"bestvideo[height={height}]+bestaudio/best[height<={height}]/best"
        if fid and opt.get("has_audio"):
            fmt = f"{fid}/{fallback}"
        elif fid:
            fmt = f"{fid}+bestaudio[ext=m4a]/{fid}+bestaudio/{fallback}"
        else:
            fmt = fallback
        outtmpl = f"%(title)s [{label}].%(ext)s"
        postprocessors = []

    ydl_opts = {
        **YDL_BASE,
        "format": fmt,
        "outtmpl": outtmpl,
        "paths": {"home": str(folder)},
        "merge_output_format": "mp4",
        "trim_file_name": 150,
        "postprocessors": postprocessors,
        "progress_hooks": [on_progress],
        "postprocessor_hooks": [on_postprocess],
        "post_hooks": [on_done],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except BaseException as exc:  # noqa: BLE001 - report every failure to the UI
        if job["_cancel"]:
            for name in parts_seen:
                for leftover in (name, name + ".part", name + ".ytdl"):
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass
            job.update(status="cancelled", stage="Cancelled", speed=None, eta=None)
        else:
            job.update(status="error", stage="Failed", error=clean_error(exc), speed=None, eta=None)
        return

    path = job.get("filepath")
    job.update(
        status="done",
        stage="Saved",
        percent=100.0,
        speed=None,
        eta=None,
        filename=os.path.basename(path) if path else None,
        filesize=os.path.getsize(path) if path and os.path.exists(path) else None,
    )


# --------------------------------------------------------------------------- main

if __name__ == "__main__":
    address = f"http://127.0.0.1:{PORT}"
    print(f"\n  Video downloader running at {address}\n  Saving to {DEFAULT_DIR}\n  Press Ctrl+C to stop.\n")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(address)).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False)
