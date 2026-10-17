"""
server.py — Local One Piece streaming server.

  python server.py            ->  http://localhost:8000

Serves the whole episode library with seeking (HTTP Range), tracks watch
progress (resume where you left off), and downloads missing episodes in the
background — several at a time, with live speed/cancel support, and playable
*while* they download (HLS from the in-flight segments).
"""
import json, math, os, queue, shutil, threading, time
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
_jobs = {}                           # ep -> job dict (see _new_job)
WATCH_AFTER_SEC = 300                # >5 min watched -> marked as watched
KEEP_WATCHED = 2                     # keep only N most recent watched on disk
_queue = queue.Queue()               # episodes waiting to be downloaded
DL_PARALLEL = 2                      # episodes downloaded at the same time
ACTIVE_STATES = ("queued", "resolving", "downloading", "joining", "remuxing")
_meta_cache = {}                     # ep -> (mtime, sidecar json)
_size_cache = {"scan": 0.0, "sizes": {}}   # refreshed at most every 2s
_cleanup_state = {"t": 0.0}          # last auto-delete sweep


def _new_job():
    return {"state": "queued", "done": 0, "total": 0, "bytes": 0, "speed": 0.0,
            "error": None, "cancel": False, "_lt": 0.0, "_lb": 0}


def _media_sizes():
    """One os.scandir pass for all episode sizes, cached for 2 seconds.
    Replaces ~1150 os.path.getsize calls per /api/episodes poll."""
    now = time.time()
    if now - _size_cache["scan"] > 2:
        sizes = {}
        try:
            for e in os.scandir(MEDIA):
                if e.name.endswith(".mp4") and e.is_file():
                    try:
                        sizes[e.name] = e.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
        _size_cache["sizes"] = sizes
        _size_cache["scan"] = now
    return _size_cache["sizes"]


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


def seg_dir(ep):
    return os.path.join(MEDIA, f"ep{ep:04d}.segs")


def sub_path(ep):
    return os.path.join(MEDIA, f"ep{ep:04d}.en.vtt")


def _is_watched(rec):
    if rec.get("watched") or rec.get("watched_ever"):
        return True
    d = rec.get("duration", 0)
    t = rec.get("time", 0)
    return t > WATCH_AFTER_SEC or (d > 0 and t / d >= 0.95)


def _sidecar(ep):
    return os.path.join(MEDIA, f"ep{ep:04d}.json")


def _write_sidecar(ep, m):
    tmp = _sidecar(ep) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f)
    os.replace(tmp, _sidecar(ep))


def _read_meta(ep):
    """Read the sidecar JSON, cached by mtime so /api/episodes polling
    doesn't re-read hundreds of small files every 2 seconds."""
    p = _sidecar(ep)
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return {}
    c = _meta_cache.get(ep)
    if c and c[0] == mt:
        return c[1]
    try:
        m = json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}
    _meta_cache[ep] = (mt, m)
    return m


def _job_active(ep):
    j = _jobs.get(ep)
    return bool(j and j.get("state") in ACTIVE_STATES)


def _build_meta(ep):
    """Fetch title/intro/outro/subs for an already-downloaded episode (backfill)."""
    embed = f"{dl.BASE}/embed/hd-2/ani/{ANIME_ID}/{ep}/dub"
    try:
        meta = dl.open_embed(embed)
    except Exception:
        return
    m = {
        "title": dl.clean_title(meta.get("title", "")) or f"Episode {ep}",
        "intro": meta.get("intro") or {"start": 0, "end": 0},
        "outro": meta.get("outro") or {"start": 0, "end": 0},
    }
    if meta.get("vtt"):
        if dl.download_vtt(meta["vtt"], embed, sub_path(ep)):
            m["sub"] = True
    _write_sidecar(ep, m)


def _make_thumb(ep):
    dl.make_thumb(ep_path(ep), os.path.join(MEDIA, f"ep{ep:04d}.jpg"), at=30)


def _backfill():
    """Fill missing thumbnails/metadata for episodes downloaded earlier."""
    time.sleep(3)
    for f in sorted(os.listdir(MEDIA)):
        if not f.endswith(".mp4"):
            continue
        ep = int(f[2:6])
        if not os.path.exists(_sidecar(ep)):
            _build_meta(ep)
        time.sleep(1)


def _cleanup_watched():
    """Keep only the KEEP_WATCHED most recent watched episodes on disk;
    delete older watched files (progress records are kept for re-download)."""
    if not _auto_delete_enabled():
        return []
    with _lock:
        p = _load_progress()
        watched = [(k, v) for k, v in p.items()
                   if _is_watched(v) and ep_exists(int(k))]
        watched.sort(key=lambda kv: kv[1].get("updated", 0), reverse=True)
        removed = []
        for k, _ in watched[KEEP_WATCHED:]:
            ep = int(k)
            try:
                os.remove(ep_path(ep))
                removed.append(ep)
            except OSError:
                pass
            for ext in (".json", ".en.vtt", ".jpg"):
                try:
                    os.remove(os.path.join(MEDIA, f"ep{ep:04d}{ext}"))
                except OSError:
                    pass
            shutil.rmtree(seg_dir(ep), ignore_errors=True)
        return removed


def _maybe_cleanup(force=False):
    """Auto-delete sweep, but at most every 15s — it used to run on every
    /api/episodes poll and every progress ping, re-reading progress.json
    hundreds of times a minute for nothing."""
    now = time.time()
    if not force and now - _cleanup_state["t"] < 15:
        return []
    _cleanup_state["t"] = now
    return _cleanup_watched()


def _run_job(ep, job):
    """Resolve + download + finalize one episode. Runs on a worker thread."""
    cfg = _cfg()
    embed = f"{dl.BASE}/embed/hd-2/ani/{ANIME_ID}/{ep}/dub"
    job["state"], job["error"] = "resolving", None
    meta = dl.open_embed(embed, browser_path=cfg.get("dl_browser_path"))
    master = meta.get("master")
    if not master:
        job["state"], job["error"] = "error", meta.get(
            "error", "no stream found on source server")
        return

    # sidecar goes down early (title/intro/outro) so the UI and a live
    # playback session have real names + skip timestamps right away
    m = {"title": dl.clean_title(meta.get("title", "")) or f"Episode {ep}",
         "intro": meta.get("intro") or {"start": 0, "end": 0},
         "outro": meta.get("outro") or {"start": 0, "end": 0}}
    _write_sidecar(ep, m)
    if meta.get("vtt"):
        try:
            if dl.download_vtt(meta["vtt"], embed, sub_path(ep)):
                m["sub"] = True
                _write_sidecar(ep, m)
        except Exception:
            pass

    job["state"], job["done"], job["total"], job["bytes"] = "downloading", 0, 0, 0

    def prog(done, total, nbytes):
        job["done"], job["total"], job["bytes"] = done, total, nbytes
        now = time.monotonic()
        lt = job["_lt"]
        if lt and now > lt:
            inst = (nbytes - job["_lb"]) / (now - lt)
            job["speed"] = inst if not job["speed"] else job["speed"] * .6 + inst * .4
        job["_lt"], job["_lb"] = now, nbytes

    def phase(p):
        if p in ("joining", "remuxing"):
            job["state"] = p

    ok = dl.download_episode(master, embed, ep_path(ep), segdir=seg_dir(ep),
                             on_progress=prog, on_phase=phase,
                             should_cancel=lambda: bool(job.get("cancel")),
                             workers=int(cfg.get("dl_workers", dl.WORKERS)))
    if job.get("cancel"):
        job["state"] = "cancelled"     # partial segments kept for a resume
        return
    if not ok:
        job["state"], job["error"] = "error", "ffmpeg remux failed"
        return

    job["state"], job["speed"] = "done", 0.0
    _write_sidecar(ep, m)
    # give a live-HLS viewer ~30s to hop over to the finished mp4, then drop
    # the segment files
    t = threading.Timer(30, shutil.rmtree, args=(seg_dir(ep),),
                        kwargs={"ignore_errors": True})
    t.daemon = True
    t.start()


def _download_worker():
    """A few episodes download at once; each one still fetches its segments
    through a bounded pool, so the source isn't hammered unreasonably."""
    while True:
        ep = _queue.get()
        job = _jobs.get(ep)
        if not job:
            continue
        try:
            _run_job(ep, job)
        except dl.Cancelled:
            job["state"] = "cancelled"
        except Exception as e:
            job["state"], job["error"] = "error", str(e)[:200]


def _ensure_workers():
    global _workers_started
    started = getattr(_ensure_workers, "started", False)
    if not started:
        _ensure_workers.started = True
        for _ in range(DL_PARALLEL):
            threading.Thread(target=_download_worker, daemon=True).start()


def _sweep_stale_segdirs():
    """On boot: drop segment folders for episodes whose mp4 exists (leftover
    grace timer). Folders without an mp4 are kept — they're a crashed
    download and the next retry resumes from them."""
    try:
        for e in os.scandir(MEDIA):
            if e.name.endswith(".segs") and e.is_dir():
                try:
                    ep = int(e.name[2:6])
                except ValueError:
                    continue
                if ep_exists(ep):
                    shutil.rmtree(e.path, ignore_errors=True)
    except OSError:
        pass


@app.get("/")
def home():
    return send_from_directory("static", "index.html")


@app.get("/style.css")
def css():
    return send_from_directory("static", "style.css")


@app.get("/app.js")
def js():
    return send_from_directory("static", "app.js")


@app.get("/vendor/<path:name>")
def vendor(name):
    return send_from_directory("static", "vendor/" + name)


@app.get("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json", mimetype="application/manifest+json")


@app.get("/icons/<path:name>")
def icons(name):
    return send_from_directory("static", "icons/" + name)


@app.get("/api/episodes")
def episodes():
    _maybe_cleanup()
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
    cfg = _cfg()
    last = int(cfg.get("last_episode", DEFAULT_LAST_EP))
    sizes = _media_sizes()
    out = []
    for ep in range(1, last + 1):
        p = prog.get(str(ep), {})
        t = p.get("time", 0)
        d = p.get("duration", 0)
        pct = round(100 * t / d, 1) if d else 0
        watched = _is_watched(p)
        fname = f"ep{ep:04d}.mp4"
        exists = fname in sizes
        job = _jobs.get(ep)
        active = bool(job and job.get("state") in ACTIVE_STATES)
        meta = _read_meta(ep) if (exists or active) else {}
        out.append({
            "ep": ep,
            "exists": exists,
            "size": sizes.get(fname, 0),
            "time": t, "duration": d, "pct": pct, "watched": watched,
            "title": meta.get("title") or f"Episode {ep}",
            "intro": meta.get("intro") or {"start": 0, "end": 0},
            "outro": meta.get("outro") or {"start": 0, "end": 0},
            "sub": bool(meta.get("sub")) and os.path.exists(sub_path(ep)),
            "job": None if not job else {
                "state": job["state"],
                "done": job["done"], "total": job["total"],
                "bytes": job["bytes"], "speed": round(job["speed"], 1),
                "error": job["error"],
                "sub": os.path.exists(sub_path(ep)),
            },
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
    p = sub_path(ep)
    if not os.path.exists(p):
        abort(404)
    return send_file(p, mimetype="text/vtt")


@app.get("/hls/<int:ep>/index.m3u8")
def hls_index(ep):
    """Live playlist for an episode that is still downloading: lists the
    contiguous run of segments already on disk, no ENDLIST until complete.
    hls.js reloads it as it grows -> watch-while-downloading."""
    sd = seg_dir(ep)
    try:
        info = json.load(open(os.path.join(sd, "segments.json"), encoding="utf-8"))
    except Exception:
        abort(404)
    segs = info.get("segs") or []
    total = int(info.get("total") or 0)
    if not segs or not total:
        abort(404)
    have = 0
    for s in segs:
        if os.path.exists(os.path.join(sd, f"seg{int(s['n']):05d}.ts")):
            have += 1
        else:
            break
    if not have:
        abort(404)                     # nothing playable yet, client retries
    maxdur = max((float(s.get("dur") or 0) for s in segs), default=0.0) or 10.0
    lines = ["#EXTM3U", "#EXT-X-VERSION:3",
             f"#EXT-X-TARGETDURATION:{max(1, math.ceil(maxdur))}",
             "#EXT-X-MEDIA-SEQUENCE:0"]
    for s in segs[:have]:
        lines.append(f"#EXTINF:{float(s.get('dur') or 0):.3f},")
        lines.append(f"/hls/{ep}/seg/{int(s['n'])}")
    if have >= total:
        lines.append("#EXT-X-ENDLIST")
    resp = app.response_class("\n".join(lines) + "\n",
                              mimetype="application/vnd.apple.mpegurl")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/hls/<int:ep>/seg/<int:n>")
def hls_seg(ep, n):
    p = os.path.join(seg_dir(ep), f"seg{n:05d}.ts")
    if not os.path.exists(p):
        abort(404)
    return send_file(p, mimetype="video/mp2t", max_age=3600, conditional=True)


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
        # sticky: once earned (>5min or ~finished), it stays watched on rewatch
        if _is_watched(cur):
            cur["watched_ever"] = True
        p[str(ep)] = cur
        _save_progress(p)
    removed = _maybe_cleanup()
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
    removed = _maybe_cleanup(force=True)
    return jsonify({"ok": True, "watched": cur["watched"], "removed": removed})


@app.get("/api/settings")
def get_settings():
    c = _cfg()
    return jsonify({
        "auto_delete": bool(c.get("auto_delete", True)),
        "last_episode": int(c.get("last_episode", DEFAULT_LAST_EP)),
        "dl_parallel": int(c.get("dl_parallel", DL_PARALLEL)),
        "dl_workers": int(c.get("dl_workers", dl.WORKERS)),
    })


@app.post("/api/settings")
def set_settings():
    d = request.get_json(force=True, silent=True) or {}
    with _lock:
        c = _cfg()
        if "auto_delete" in d:
            c["auto_delete"] = bool(d["auto_delete"])
        _save_cfg(c)
    return jsonify({"ok": True, "auto_delete": bool(c["auto_delete"])})


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


def _enqueue(ep):
    _jobs[ep] = _new_job()
    _queue.put(ep)
    _ensure_workers()


@app.post("/api/download/<int:ep>")
def download(ep):
    if ep_exists(ep):
        return jsonify({"ok": True, "state": "exists"})
    job = _jobs.get(ep)
    if job and job["state"] in ACTIVE_STATES:
        return jsonify({"ok": True, "state": job["state"]})
    _enqueue(ep)
    return jsonify({"ok": True, "state": "queued"})


@app.post("/api/cancel/<int:ep>")
def cancel(ep):
    """Ask a queued/running download to stop; partial segments are kept so
    the next retry resumes from where it stopped."""
    job = _jobs.get(ep)
    if not job or job["state"] not in ACTIVE_STATES:
        return jsonify({"ok": False, "state": job["state"] if job else None})
    job["cancel"] = True
    return jsonify({"ok": True, "state": job["state"]})


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
    cfg = _cfg()
    last = int(cfg.get("last_episode", DEFAULT_LAST_EP))
    queued = []
    ep = from_ep + 1
    while len(queued) < count and ep <= last:
        if not ep_exists(ep) and not _job_active(ep):
            _enqueue(ep)
            queued.append(ep)
        ep += 1
    return jsonify({"ok": True, "queued": queued})


@app.get("/api/stats")
def stats():
    sizes = _media_sizes()
    total = sum(sizes.values())
    prog = _load_progress()
    watched = sum(1 for v in prog.values() if _is_watched(v))
    return jsonify({"downloaded": len(sizes),
                    "bytes": total,
                    "watched": watched,
                    "active": sum(1 for j in _jobs.values()
                                  if j.get("state") in ACTIVE_STATES)})


@app.get("/api/continue")
def continue_watching():
    """Resume = the highest episode you have started, even if finished."""
    prog = _load_progress()
    best = None
    for k in prog:
        try:
            ep = int(k)
        except ValueError:
            continue
        if best is None or ep > best:
            best = ep
    if best is None:
        return jsonify({'ep': None, 'time': -1, 'exists': False, 'finished': False})
    rec = prog.get(str(best), {})
    t = rec.get('time', 0)
    d = rec.get('duration', 0)
    finished = bool(d and t / d >= 0.95)
    return jsonify({'ep': best, 'time': 0 if finished else t,
                    'exists': ep_exists(best), 'finished': finished})


@app.post('/api/started/<int:ep>')
def started(ep):
    """Mark an episode as started (creates a zero-progress record if none)."""
    with _lock:
        p = _load_progress()
        if str(ep) not in p:
            p[str(ep)] = {'time': 0, 'duration': 0, 'updated': time.time()}
            _save_progress(p)
    return jsonify({'ok': True})


if __name__ == "__main__":
    _sweep_stale_segdirs()
    threading.Thread(target=_backfill, daemon=True).start()
    print("One Piece server  ->  http://localhost:8000")
    app.run(host="0.0.0.0", port=8000, threaded=True, debug=False)
