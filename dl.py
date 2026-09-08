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
from playwright.sync_api import sync_playwright

BASE = "https://cdn.4animo.xyz"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
WORKERS = 6


def strip_png(data):
    """The CDN wraps MPEG-TS in a PNG stub; the TS data follows the IEND chunk."""
    i = data.find(b"IEND")
    if i != -1 and len(data) > i + 8 and data[i + 8:i + 9] == b"\x47":
        return data[i + 8:]
    return data


def open_embed(embed_url, timeout_s=60):
    """Open the embed page once and collect everything: master playlist URL,
    episode title, intro timestamps and the subtitle track URL."""
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_context(user_agent=UA).new_page()

        def on_resp(r):
            if "getSources" in r.url and "master" not in out:
                try:
                    j = r.json()
                    f = j["sources"][0]["file"]
                    if f.startswith("/p?t="):
                        out["master"] = BASE + f
                        out["intro"] = j.get("intro") or {"start": 0, "end": 0}
                        tr = j.get("tracks") or []
                        vf = tr[0].get("file", "") if tr else ""
                        out["vtt"] = BASE + vf if vf.startswith("/p?t=") else None
                except Exception:
                    pass

        pg.on("response", on_resp)
        pg.goto(embed_url, wait_until="domcontentloaded", timeout=90000)
        for _ in range(timeout_s):
            if "master" in out:
                break
            pg.wait_for_timeout(1000)
        try:
            out["title"] = pg.title()
        except Exception:
            out["title"] = ""
        b.close()
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

    uris = [l for l in s.get(master_url, timeout=30).text.splitlines()
            if l and not l.startswith("#")]
    if not uris:
        print("  empty master playlist")
        return False
    variant = uris[0]
    lines = s.get(variant, timeout=30).text.splitlines()
    segs, i = [], 0
    while i < len(lines):
        if lines[i].startswith("#EXTINF") and i + 1 < len(lines):
            segs.append(lines[i + 1])
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
    os.makedirs(a.out, exist_ok=True)
    ok = 0

    for ep in eps:
        embed = f"{BASE}/embed/{a.server}/ani/{a.anime}/{ep}/{lang}"
        print(f"\n=== ep {ep} | {embed}")
        try:
            meta = open_embed(embed)
            master = meta.get("master")
        except Exception as e:
            print("  embed open error:", e)
            continue
        if not master:
            print("  no getSources/master found (episode may not exist on this server).")
            continue
        print("  master ok")
        outfile = os.path.join(a.out, f"ep{ep:04d}.mp4")
        if download_episode(master, embed, outfile, test=a.test):
            ok += 1
            print("  saved:", outfile, f"({os.path.getsize(outfile)/1e6:.1f} MB)")

    print(f"\nfinished: {ok}/{len(eps)} episode(s) saved")


if __name__ == "__main__":
    main()