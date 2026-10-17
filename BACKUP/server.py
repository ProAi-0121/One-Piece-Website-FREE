"""
server.py — Local One Piece streaming server.

  python server.py            ->  http://localhost:8000

Serves the whole episode library with seeking (HTTP Range), tracks watch
progress (resume where you left off), and can download missing episodes
straight from the source in the background.
"""
import json, os, queue, threading, time
from flask import Flask, request, jsonify, send_file, send_from_directory, abort

import dl

APP_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIA = os.path.join(APP_DIR, "media")
PROG_FILE = os.path.join(APP_DIR, "progress.json")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
ANIME_ID = "21"                      # One Piece in the embed path
DEFAULT_LAST_EP = 1150

os.makedirs(MEDIA, exist_ok=True)
app = Flask(__name__, static_folder="static")

_lock = threading.Lock()
_jobs = {}                           # ep -> {"done":n,"total":t,"state":str,"error":None}
WATCH_AFTER_SEC = 120                # >2 min watched -> marked as watched
KEEP_WATCHED = 2                     # keep only N most recent watched on disk
_queue = queue.Queue()               # episodes waiting to be downloaded
_worker_started = False


def _cfg():
    if os.path.exists(CONFIG_FILE):
        try:
            return json.load(open(CONFIG_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {"last_episode": DEFAULT_LAST_EP, "auto_delete": True}


def _save_cfg(c):
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f)
    os.replace(tmp, CONFIG_FILE)


def _auto_delete_enabled():
    return bool(_cfg().get("auto_delete", True))


def _load_progress():
    if os.path.exists(PROG_FILE):
        try:
            return json.load(open(PROG_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_progress(p):
    tmp = PROG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(p, f)
    os.replace(tmp, PROG_FILE)


def ep_path(ep):
    return os.path.join(MEDIA, f"ep{ep:04d}.mp4")


def ep_exists(ep):
    return os.path.exists(ep_path(ep))


def _is_watched(rec):
    if rec.get("watched") or rec.get("watched_ever"):
        return True
    d = rec.get("duration", 0)
    t = rec.get("time", 0)
    return t > WATCH_AFTER_SEC or (d > 0 and t / d >= 0.95)


def _cleanup_watched():
    """Keep only the KEEP_WATCHED most recent watched episodes on disk."""
    if not _auto_delete_enabled():
        return []
    with _lock:
        p = _load_progress()
        watched = [(k, v) for k, v in p.items()
                   if _is_watched(v) and ep_exists(int(k))]
        watched.sort(key=lambda kv: kv[1].get("updated", 0), reverse=True)
        removed = []
        for k, _ in watched[KEEP_WATCHED:]:
            ep = int(k_)
            try:
                os.remove(ep_path(ep))
                removed.append(ep)
            except OSError:
                pass
        return removed


def _queue_worker():
    """One download at a time — gentle on the source and easy to track."""
    while True:
        ep = _queue.get()
        job = _jobs.setdefault(ep, {})
        try:
            embed = f"{dl.BASE}/embed/hd-2/ani/{ANIME_ID}/{ep}/dub"
            job["state"] = "resolving"
        except Exception as e:
            job["state"], job["error"] = "error", str(e)[:200]


def _ensure_worker():
    global _worker_started
    if not _worker_started:
        _worker_started = True
        threading.Thread(target=_queue_worker, daemon=True).start()


@app.get("/")
def home():
    print('Home page accessed')
    return send_from_directory("static", "index.html")


@app.get("/style.css")
def css():
    return send_from_directory("static", "style.css")


@app.get("/app.js")
def js():
    return send_from_directory("static", "app.js")


@app.get("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json", mimetype="application/manifest+json")


@app.get("/icons/<path:name>")
def icons(name):
    return send_from_directory("static", "icons/" + name)


@app.get("/api/episodes")
def episodes():
    cfg = _cfg()
    removed = _cleanup_watched()
    prog = _load_progress()
    # backfill: make every already-earned watched mark sticky/permanent
    changed = False
    for k, v in prog.items():
        if _is_watched(v) and not v.get("watched_ever"):
            v["watched_ever"] = True
            changed = True
    if changed:
        with _lock:
            _save_progress(prog)
    last = int(cfg.get("last_episode", DEFAULT_LAST_EP))
    out = []
    for ep in range(1, last + 1):
        p = prog.get(str(ep), {})
        t = p.get("time", 0)
        d = p.get("duration", 0)
        pct = round(100 * t / d, 1) if d else 0
        watched = _is_watched(p)
        job = _jobs.get(ep)
        out.append({
            "ep": ep,
            "exists": ep_exists(ep),
            "size": os.path.getsize(ep_path(ep)) if ep_exists(ep) else 0,
            "time": t, "duration": d, "pct": pct, "watched": watched,
            "job": {k: job[k] for k in ("state", "done", "total", "error")} if job else None,
        })
    return jsonify(out)


@app.get("/thumb/<int:ep>")
def thumb(ep):
    p = os.path.join(MEDIA, f"ep{ep:04d}.jpg")
    if not os.path.exists(p):
        abort(404)
    return send_file(p, mimetype="image/jpeg", max_age=86400)


@app.get("/sub/<int:ep>")
def sub(ep):
    p = os.path.join(MEDIA, f"ep{ep:04d}.en.vtt")
    if not os.path.exists(p):
        abort(404)
    return send_file(p, mimetype="text/vtt")


@app.post("/api/progress")
def progress():
    d = request.get_json(force=True, silent=True) or {}
    try:
        ep = int(d["ep"])
        t = max(0.0, float(d.get("time", 0)))
        dur = max(0.0, float(d.get("duration", 0)))
    except Exception:
        return jsonify({"ok": False}), 400
    with _lock:
        p = _load_progress()
        cur = p.get(str(ep), {})
        cur.update({"time": t, "duration": dur, "updated": time.time()})
        # sticky: once earned (>2min or ~finished), it stays watched on rewatch
        if _is_watched(cur):
            cur["watched_ever"] = True
        p[str(ep)] = cur
        _save_progress(p)
    removed = _cleanup_watched()
    return jsonify({"ok": True, "removed": removed})


@app.post("/api/watched/<int:ep>")
def mark_watched(ep):
    with _lock:
        p = _load_progress()
        cur = p.get(str(ep), {})
        cur["watched"] = not cur.get("watched", False)
        cur["watched_ever"] = cur["watched"]   # unmark clears the sticky flag too
        cur["updated"] = time.time()
        p[str(ep)] = cur
        _save_progress(p)
    removed = _cleanup_watched()
    return jsonify({"ok": True, "watched": cur["watched"], "removed": removed})


@app.post("/api/reset/<int:ep>")
def reset(ep):
    with _lock:
        p = _load_progress()
        p.pop(str(ep), None)
        _save_progress(p)
    return jsonify({"ok": True})


@app.get("/video/<int:ep>")
def video(ep):
    if not ep_exists(ep):
        abort(404)
    # conditional=True -> automatic HTTP Range support (fast seeking)
    return send_file(ep_path(ep), mimetype="video/mp4",
                     conditional=True, as_attachment=False)


@app.post("/api/download/<int:ep>")
def download(ep):
    if ep_exists(ep):
        return jsonify({"ok": True, "state": "exists"})
    job = _jobs.get(ep)
    if job and job["state"] not in ("done", "error"):
        return jsonify({"ok": True, "state": job["state"]})
    _jobs[ep] = {"state": "queued", "done": 0, "total": 0, "error": None}
    _queue.put(ep)
    _ensure_worker()
    return jsonify({"ok": True, "state": "queued"})


@app.post("/api/download/batch")
def download_batch():
    """Queue the next N missing episodes after <from_ep> (default: last watched)."""
    d = request.get_json(force=True, silent=True) or {}
    try:
        count = max(1, min(int(d.get("count", 5)), 50))
        from_ep = int(d.get("from", 0))
    except Exception:
        return jsonify({"ok": False}), 400
    if not from_ep:
        cont = continue_watching().get_json()
        from_ep = (cont.get("ep") or 0)
    queued = []
    ep = from_ep + 1
    while len(queued) < count and ep <= DEFAULT_LAST_EP:
        if not ep_exists(ep):
            job = _jobs.get(ep)
            busy = job and job["state"] not in ("done", "error")
            if not busy:
                _jobs[ep] = {"state": "queued", "done": 0, "total": 0, "error": None}
                _queue.put(ep)
                queued.append(ep)
        ep += 1
    _ensure_worker()
    return jsonify({"ok": True, "queued": queued})


@app.get("/api/stats")
def stats():
    files = [f for f in os.listdir(MEDIA) if f.endswith(".mp4")]
    total = sum(os.path.getsize(os.path.join(MEDIA, f)) for f in files)
    prog = _load_progress()
    watched = sum(1 for v in prog.values() if _is_watched(v))
    return jsonify({"downloaded": len(files),
                    "bytes": total,
                    "watched": watched,
                    "active": sum(1 for j in _jobs.values()
                                  if j.get("state") not in ("done", "error"))})


@app.get("/api/continue")

def continue_watching():
    prog = _load_progress()
    best, best_t = None, -1
    for k, v in prog.items():
        d = v.get("duration", 0)
        t = v.get("time", 0)
        if d and 0 < t and t / d < 0.95 and t > best_t and ep_exists(int(k)):
            best, best_t = int(k), t
    if best:
        return jsonify({"ep": best, "time": best_t, "exists": True})
    recent, recent_ts = None, -1
    for k, v in prog.items():
        ts = v.get("updated", 0)
        if ts > recent_ts:
            recent_ts, recent = ts, int(k)
    nxt = (recent + 1) if recent else None
    if nxt:
        return jsonify({"ep": nxt, "time": 0, "exists": ep_exists(nxt)})
    return jsonify({"ep": None, "time": -1, "exists": False})


if __name__ == "__main__":
    threading.Thread(target=_backfill, daemon=True).start()
    print("One Piece server  ->  http://localhost:8000")
    app.run(host="0.0.0.0", port=8000, threaded=True, debug=False)