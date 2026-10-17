"""
dl.py — Direct downloader for hianime.lol episodes via the 4animo embed CDN.

Flow:
  1. GET the embed page (plain HTTP — no browser needed) -> getSources token
     -> master HLS playlist URL.
  2. Plain requests fetches master -> variant -> segments, in parallel.
  3. IMPORTANT: every "segment" is served as a PNG image with the real
     MPEG-TS data APPENDED AFTER THE IEND CHUNK. We strip the PNG prefix.
  4. The clean .ts segments go straight into ffmpeg's concat demuxer,
     which remuxes them to .mp4 (-c copy). No giant intermediate .ts file,
     so finalizing costs one read + one write instead of three.

Segments are written into a per-episode "segdir" that the server keeps
serving over HLS while the download runs (watch-while-downloading), and a
finished segment is never fetched twice — so a failed download resumes
where it stopped instead of starting over.

Usage:
    python dl.py --ep 556
    python dl.py --ep-range 556 600
    python dl.py --ep 556 --server hd-1 --sub
    python dl.py --ep 556 --test
    python dl.py --ep 556 --out "D:\\Anime\\OP"
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter

BASE = "https://cdn.4animo.xyz"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
WORKERS = 10


class Cancelled(Exception):
    """Raised when the caller asks the download to stop."""


# Windows 8.1 can't run Playwright's bundled Chromium (needs Win10+).
# Kept for backwards compatibility: config.json's dl_browser_path is still
# accepted, but resolving streams no longer needs a browser at all.
CANDIDATE_BROWSERS = [
    os.environ.get("ONE_PIECE_BROWSER", "") or "",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\EdgeCore\Application\msedge.exe",
]


def resolve_browser_path(forced=None):
    """Return an executable path for launching, or None. (Legacy — unused.)"""
    if forced:
        return forced
    for c in CANDIDATE_BROWSERS:
        c = c.strip()
        if c and os.path.exists(c):
            return c
    return None


CANDIDATE_FFMPEG = [
    os.environ.get("ONE_PIECE_FFMPEG", "") or "",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe"),
]


def find_ffmpeg():
    """Resolve an ffmpeg executable: env var, ffmpeg.exe next to the script,
    or anything on PATH. Raises SystemExit with a clear message if none found,
    instead of a cryptic FileNotFoundError [WinError 2]."""
    forced = [c for c in CANDIDATE_FFMPEG if c]
    for c in forced:
        if os.path.exists(c):
            return c
    on_path = shutil.which("ffmpeg")
    if on_path:
        return "ffmpeg"
    raise SystemExit("ffmpeg not found. Put ffmpeg.exe next to dl.py/server.py, "
                     "add it to PATH, or set ONE_PIECE_FFMPEG to its full path.")


def _run_ffmpeg(args):
    """subprocess.run for ffmpeg that reports 'ffmpeg not found' clearly on
    Windows instead of crashing with FileNotFoundError (WinError 2)."""
    try:
        return subprocess.run([find_ffmpeg()] + args, capture_output=True)
    except FileNotFoundError:
        print("ffmpeg not found — install it or set ONE_PIECE_FFMPEG.")
        return None


def make_session(referer, workers=WORKERS):
    """Session with a connection pool sized for the parallel segment workers
    (the urllib3 default of 10 would throttle anything above that)."""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": referer})
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=max(workers + 4, 16))
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def strip_png(data):
    """The CDN wraps MPEG-TS in a PNG stub; the TS data follows the IEND chunk."""
    i = data.find(b"IEND")
    if i != -1 and len(data) > i + 8 and data[i + 8:i + 9] == b"\x47":
        return data[i + 8:]
    return data


def cdn_reachable(timeout=8):
    """Quick check that the source CDN is up before doing any work."""
    try:
        r = requests.get(f"{BASE}/", timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False


def open_embed(embed_url, timeout_s=30, browser_path=None, precheck=False):
    """Resolve the master HLS playlist with plain HTTP only — no browser needed,
    so this runs on any Windows version (7/8/8.1/10/11).

    1. GET the embed page html (the getSources token is right there in it)
    2. GET /stream/getSources?t=<token>  ->  master playlist path + intro/outro
       timestamps + subtitle track + episode title
    """
    out = {}
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Referer": embed_url})

    try:
        r = sess.get(embed_url, timeout=timeout_s)
    except Exception as e:
        out["error"] = "embed unreachable: %s" % str(e)[:120]
        return out
    if r.status_code != 200:
        out["error"] = "embed returned HTTP %s (source may be down)" % r.status_code
        return out

    tok = re.search(r"getSources\?t=([A-Za-z0-9_\-.]+)", r.text)
    if not tok:
        out["error"] = "embed page has no stream token (source down or changed)"
        return out

    t = re.search(r"<title>(.*?)</title>", r.text, re.S)
    if t:
        out["title"] = t.group(1).strip()

    try:
        g = sess.get(f"{BASE}/stream/getSources?t={tok.group(1)}", timeout=timeout_s)
        if g.status_code != 200:
            out["error"] = "getSources returned HTTP %s" % g.status_code
            return out
        j = g.json()
        f = j["sources"][0]["file"]
        if not f.startswith("/p?t="):
            out["error"] = "unexpected source format"
            return out
        out["master"] = BASE + f
        out["intro"] = j.get("intro") or {"start": 0, "end": 0}
        out["outro"] = j.get("outro") or {"start": 0, "end": 0}
        tr = j.get("tracks") or []
        vf = tr[0].get("file", "") if tr else ""
        out["vtt"] = BASE + vf if vf.startswith("/p?t=") else None
    except Exception as e:
        out["error"] = "getSources parse failed: %s" % str(e)[:120]
    return out


def clean_title(raw):
    """'Ep 556: Unveiled! ... - ReCloud' -> 'Unveiled! ...'"""
    t = re.sub(r"^\s*Ep\.?\s*\d+\s*:\s*", "", raw or "")
    t = re.sub(r"\s*-\s*ReCloud\s*$", "", t)
    return t.strip()


def download_vtt(url, referer, path):
    s = make_session(referer, workers=2)
    r = s.get(url, timeout=60)
    if r.status_code == 200 and b"WEBVTT" in r.content[:200]:
        with open(path, "wb") as f:
            f.write(r.content)
        return True
    return False


def make_thumb(video_path, out_path, at=30):
    """Grab a poster frame with ffmpeg."""
    res = _run_ffmpeg(["-y", "-ss", str(at), "-i", video_path,
                       "-frames:v", "1", "-vf", "scale=480:-1", out_path])
    return res is not None and res.returncode == 0 and os.path.exists(out_path)


def _parse_variant(text):
    """[(url, duration)] from an m3u8 variant playlist."""
    segs, i = [], 0
    lines = text.splitlines()
    while i < len(lines):
        if lines[i].startswith("#EXTINF"):
            dur = 0.0
            m = re.search(r"#EXTINF:([\d.]+)", lines[i])
            if m:
                dur = float(m.group(1))
            if i + 1 < len(lines):
                u = lines[i + 1].strip()
                if u.startswith("http"):
                    segs.append((u, dur))
                i += 2
                continue
        i += 1
    return segs


def _write_segments_json(segdir, total, segs):
    with open(os.path.join(segdir, "segments.json"), "w", encoding="utf-8") as f:
        json.dump({"total": total,
                   "segs": [{"n": n, "dur": d} for n, (u, d) in enumerate(segs, 1)]}, f)


def download_episode(master_url, referer, outfile, segdir=None, test=False,
                     on_progress=None, on_phase=None, should_cancel=None,
                     workers=None):
    """Download all segments (parallel, resumable) and remux to mp4.

    segdir           where clean .ts segments live; persisted by the server so
                     partial downloads can be watched over HLS and resumed.
                     None -> a throwaway temp dir (CLI use).
    on_progress      called as on_progress(done, total, bytes_done)
    on_phase         called with "remuxing" / "joining" (fallback path)
    should_cancel    polled between retries and segments; raise Cancelled
    """
    s = make_session(referer, workers or WORKERS)
    tmp_owner = segdir is None
    if segdir is None:
        segdir = tempfile.mkdtemp(prefix="opseg_")
    os.makedirs(segdir, exist_ok=True)
    if should_cancel and should_cancel():
        raise Cancelled()

    try:
        mr = s.get(master_url, timeout=30)
        if mr.status_code != 200 or "#EXTM3U" not in mr.text:
            print("  master playlist fetch failed (HTTP %s)" % mr.status_code)
            return False
        uris = [l.strip() for l in mr.text.splitlines()
                if l.strip() and not l.startswith("#")]
        if not uris:
            print("  empty master playlist")
            return False
        vr = s.get(uris[0], timeout=30)
        if vr.status_code != 200 or "#EXTM3U" not in vr.text:
            print("  variant playlist fetch failed (HTTP %s)" % vr.status_code)
            return False
        segs = _parse_variant(vr.text)
        if test:
            segs = segs[:10]
        total = len(segs)
        if not total:
            print("  variant playlist has no segments")
            return False
        print(f"  {total} segment(s)")

        # manifest of the episode: total + per-segment durations, used by the
        # server to serve a live HLS playlist while segments still arrive.
        # If a stale segdir from a different playlist is lying around, wipe it.
        sj = os.path.join(segdir, "segments.json")
        try:
            if os.path.exists(sj) and json.load(open(sj, encoding="utf-8")).get("total") != total:
                for f in os.listdir(segdir):
                    if f.endswith(".ts"):
                        os.remove(os.path.join(segdir, f))
        except Exception:
            pass
        _write_segments_json(segdir, total, segs)

        def fetch(item):
            n, (url, _) = item
            target = os.path.join(segdir, f"seg{n:05d}.ts")
            if os.path.exists(target) and os.path.getsize(target) > 0:
                return os.path.getsize(target)      # resume: already have it
            last = None
            for attempt in range(4):                # transient CDN hiccups
                if should_cancel and should_cancel():
                    raise Cancelled()
                try:
                    r = s.get(url, timeout=120)
                    if r.status_code == 200:
                        data = strip_png(r.content)
                        if not data.startswith(b"\x47"):
                            raise ValueError(f"seg {n}: no TS data behind PNG stub")
                        part = target + ".part"
                        with open(part, "wb") as f:
                            f.write(data)
                        os.replace(part, target)    # atomic: never a torn file
                        return len(data)
                    last = RuntimeError(f"seg {n}: HTTP {r.status_code}")
                except Cancelled:
                    raise
                except Exception as e:
                    last = e
                time.sleep(1 + attempt * 1.5)
            raise last

        workers = max(2, workers or WORKERS)
        done, nbytes = 0, 0
        with ThreadPoolExecutor(workers) as ex:
            futures = [ex.submit(fetch, it) for it in enumerate(segs, 1)]
            for fut in as_completed(futures):
                nbytes += fut.result()
                done += 1
                if on_progress:
                    on_progress(done, total, nbytes)
                elif done % 25 == 0 or done == total:
                    print(f"    {done}/{total}")

        if should_cancel and should_cancel():
            raise Cancelled()

        if on_phase:
            on_phase("remuxing")
        lst = os.path.join(segdir, "list.txt")
        with open(lst, "w", encoding="utf-8") as f:
            for n in range(1, total + 1):
                p = os.path.join(segdir, f"seg{n:05d}.ts").replace("\\", "/")
                f.write("file '%s'\n" % p)
        res = _run_ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", lst,
                           "-c", "copy", "-bsf:a", "aac_adtstoasc", outfile])
        if res is not None and res.returncode == 0:
            return True

        # fallback for odd inputs the concat demuxer chokes on: old-style
        # physical join into one .ts, then remux that.
        print("  concat remux failed, falling back to join+remux")
        if on_phase:
            on_phase("joining")
        with open(outfile + ".ts", "wb") as out:
            for n in range(1, total + 1):
                with open(os.path.join(segdir, f"seg{n:05d}.ts"), "rb") as f:
                    shutil.copyfileobj(f, out)
        if on_phase:
            on_phase("remuxing")
        res = _run_ffmpeg(["-y", "-i", outfile + ".ts", "-c", "copy",
                           "-bsf:a", "aac_adtstoasc", outfile])
        if res is not None and res.returncode == 0:
            os.remove(outfile + ".ts")
            return True
        print("  ffmpeg remux failed; raw .ts kept at", outfile + ".ts")
        return False
    finally:
        if tmp_owner:
            shutil.rmtree(segdir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="Download hianime episodes via 4animo embed.")
    ap.add_argument("--anime", default="21", help="anime id in the embed path (One Piece = 21)")
    ap.add_argument("--ep", type=int, help="single episode number")
    ap.add_argument("--ep-range", nargs=2, type=int, metavar=("START", "END"))
    ap.add_argument("--server", default="hd-2", help="embed server key (hd-2 = 4th DUB)")
    ap.add_argument("--sub", action="store_true", help="download SUB instead of DUB")
    ap.add_argument("--out", default="downloads", help="output folder")
    ap.add_argument("--test", action="store_true", help="fetch only 10 segments to verify")
    ap.add_argument("--workers", type=int, default=WORKERS, help="parallel segment fetches")
    ap.add_argument("--browser", default=None,
                    help="path to chrome/edge exe (legacy, no longer needed)")
    a = ap.parse_args()

    if a.ep_range:
        eps = list(range(a.ep_range[0], a.ep_range[1] + 1))
    elif a.ep:
        eps = [a.ep]
    else:
        print("Give --ep or --ep-range.")
        sys.exit(2)
    find_ffmpeg()  # clear error message if ffmpeg is missing

    lang = "sub" if a.sub else "dub"
    os.makedirs(a.out, exist_ok=True)
    ok = 0

    for ep in eps:
        embed = f"{BASE}/embed/{a.server}/ani/{a.anime}/{ep}/{lang}"
        print(f"\n=== ep {ep} | {embed}")
        meta = open_embed(embed)
        master = meta.get("master")
        if not master:
            print("  error:", meta.get("error", "no stream found"))
            continue
        print("  master ok")
        outfile = os.path.join(a.out, f"ep{ep:04d}.mp4")
        if download_episode(master, embed, outfile, test=a.test, workers=a.workers):
            ok += 1
            print("  saved:", outfile, f"({os.path.getsize(outfile)/1e6:.1f} MB)")

    print(f"\nfinished: {ok}/{len(eps)} episode(s) saved")


if __name__ == "__main__":
    main()
