"""
dl.py — Direct downloader for hianime.lol episodes via the 4animo embed CDN.

Flow:
  1. Playwright opens the embed page (needed only to obtain the getSources
     token) -> master HLS playlist URL.
  2. Plain requests fetches master -> variant -> segments.
  3. IMPORTANT: every "segment" is served as a PNG image with the real
     MPEG-TS data APPENDED AFTER THE IEND CHUNK. We strip the PNG prefix.
  4. Segments are concatenated and remuxed to .mp4 with ffmpeg (-c copy).

The 4th DUB server maps to:  https://cdn.4animo.xyz/embed/hd-2/ani/21/<ep>/dub

Usage:
    python dl.py --ep 556
    python dl.py --ep-range 556 600
    python dl.py --ep 556 --server hd-1 --sub
    python dl.py --ep 556 --test
    python dl.py --ep 556 --out "D:\\Anime\\OP"
"""
import argparse, os, re, shutil, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "https://cdn.4animo.xyz"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
WORKERS = 6

# Windows 8.1 can't run Playwright's bundled Chromium (needs Win10+).
# When the user sets dl_browser_path in config.json (or this env var), we drive
# that system Chrome/Edge instead, which runs fine on older Windows.
CANDIDATE_BROWSERS = [
    os.environ.get("ONE_PIECE_BROWSER", "") or "",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\EdgeCore\Application\msedge.exe",
]


def resolve_browser_path(forced=None):
    """Return an executable path for launching, or None to use Playwright's
    bundled Chromium. Prefers the (optional) forced path, then system browsers.
    """
    if forced:
        return forced
    for c in CANDIDATE_BROWSERS:
        c = c.strip()
        if c and os.path.exists(c):
            return c
    return None


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
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": referer})
    r = s.get(url, timeout=60)
    if r.status_code == 200 and b"WEBVTT" in r.content[:200]:
        with open(path, "wb") as f:
            f.write(r.content)
        return True
    return False


def make_thumb(video_path, out_path, at=30):
    """Grab a poster frame with ffmpeg."""
    rc = subprocess.run(
        ["ffmpeg", "-y", "-ss", str(at), "-i", video_path,
         "-frames:v", "1", "-vf", "scale=480:-1", out_path],
        capture_output=True)
    return rc.returncode == 0 and os.path.exists(out_path)


def download_episode(master_url, referer, outfile, test=False, on_progress=None):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": referer})

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
    lines = vr.text.splitlines()
    segs, i = [], 0
    while i < len(lines):
        if lines[i].startswith("#EXTINF") and i + 1 < len(lines):
            u = lines[i + 1].strip()
            if u.startswith("http"):
                segs.append(u)
            i += 2
        else:
            i += 1
    if test:
        segs = segs[:10]
    total = len(segs)
    print(f"  {total} segment(s)")

    tmp = tempfile.mkdtemp(prefix="opseg_")
    try:
        def fetch(item):
            n, url = item
            r = s.get(url, timeout=120)
            if r.status_code != 200:
                raise RuntimeError(f"seg {n}: HTTP {r.status_code}")
            with open(os.path.join(tmp, f"{n:05d}.ts"), "wb") as f:
                f.write(strip_png(r.content))

        with ThreadPoolExecutor(WORKERS) as ex:
            done = 0
            for _ in ex.map(fetch, enumerate(segs)):
                done += 1
                if on_progress:
                    on_progress(done, total)
                elif done % 25 == 0 or done == total:
                    print(f"    {done}/{total}")

        with open(outfile + ".ts", "wb") as out:
            for n in range(total):
                with open(os.path.join(tmp, f"{n:05d}.ts"), "rb") as f:
                    shutil.copyfileobj(f, out)

        rc = subprocess.run(["ffmpeg", "-y", "-i", outfile + ".ts", "-c", "copy",
                             "-bsf:a", "aac_adtstoasc", outfile],
                            capture_output=True).returncode
        if rc == 0:
            os.remove(outfile + ".ts")
            return True
        print("  ffmpeg remux failed; raw .ts kept at", outfile + ".ts")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="Download hianime episodes via 4animo embed.")
    ap.add_argument("--anime", default="21", help="anime id in the embed path (One Piece = 21)")
    ap.add_argument("--ep", type=int, help="single episode number")
    ap.add_argument("--ep-range", nargs=2, type=int, metavar=("START", "END"))
    ap.add_argument("--server", default="hd-2", help="embed server key (hd-2 = 4th DUB)")
    ap.add_argument("--sub", action="store_true", help="download SUB instead of DUB")
    ap.add_argument("--out", default="downloads", help="output folder")
    ap.add_argument("--test", action="store_true", help="fetch only 10 segments to verify")
    ap.add_argument("--browser", default=None,
                    help="path to chrome/edge exe (use this on Windows 8.1)")
    a = ap.parse_args()

    if a.ep_range:
        eps = list(range(a.ep_range[0], a.ep_range[1] + 1))
    elif a.ep:
        eps = [a.ep]
    else:
        print("Give --ep or --ep-range.")
        sys.exit(2)
    if not shutil.which("ffmpeg"):
        print("ffmpeg not found on PATH.")
        sys.exit(2)

    lang = "sub" if a.sub else "dub"
    bpath = resolve_browser_path(a.browser)
    os.makedirs(a.out, exist_ok=True)
    ok = 0

    for ep in eps:
        embed = f"{BASE}/embed/{a.server}/ani/{a.anime}/{ep}/{lang}"
        print(f"\n=== ep {ep} | {embed}")
        meta = open_embed(embed, browser_path=bpath)
        master = meta.get("master")
        if not master:
            print("  error:", meta.get("error", "no stream found"))
            continue
        print("  master ok")
        outfile = os.path.join(a.out, f"ep{ep:04d}.mp4")
        if download_episode(master, embed, outfile, test=a.test):
            ok += 1
            print("  saved:", outfile, f"({os.path.getsize(outfile)/1e6:.1f} MB)")

    print(f"\nfinished: {ok}/{len(eps)} episode(s) saved")


if __name__ == "__main__":
    main()