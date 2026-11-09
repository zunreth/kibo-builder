#!/usr/bin/env python3
"""kibo-builder — video/audio renderer: yt-dlp on GitHub Actions -> temporary host
-> callback to the bot worker.

Why this exists: keyless resolvers that hand out playable media URLs keep dying,
and the survivors serve links behind CDNs that refuse the messenger's fetchers.
A file we render ourselves is the one that reliably reaches the chat.

The messenger's 20 MB by-URL ceiling is NOT a limit on what you can build:
bigger files go to a temporary host and the bot posts the direct link.

Flow: render the best mp4 that fits --max-mb (or extract mp3) -> upload
(temporary hosts first) -> POST {tok, url, kind, fmt, size, host, exp} to the
callback URL. Kind "best"/"<height>" = video, "mp3" = audio track.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

import requests

import common as C

DEFAULT_MAX_MB = 1000     # 1 GiB — runner disk & the job time budget, not the chat
LADDER = (2160, 1440, 1080, 720, 480, 360)
TG_UA = "TelegramBot (like TwitterBot)"

# Temporary hosts first. Permanent hosting is a favour, not a dump: keep it last
# and only for files that actually fit (kappa caps at 100 MiB).
HOSTS = (
    ("sendit.sh", 10000, "1 day / 5 downloads"),
    ("temp.sh", 4096, "3 days"),
    ("x0.at", 1024, "auto-expire"),
    ("kappa.lol", 100, "permanent"),
)


def sh(args, timeout=2400):
    C.log("$ " + " ".join(args))
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        C.log("  (timeout)")
        return 1, ""
    out = (p.stdout or "") + (p.stderr or "")
    C.log(out[-4000:])
    return p.returncode, out


def probe(url):
    """Title / duration / uploader without downloading (-J chokes on big JSON, --print is tiny)."""
    rc, out = sh(["yt-dlp", "--no-playlist", "--no-warnings", "--skip-download",
                  "--print", "%(title)s\t%(duration)s\t%(uploader)s", url], timeout=300)
    if rc != 0:
        return {}
    rows = [ln for ln in out.strip().splitlines() if ln.count("\t") == 2]
    if not rows:
        return {}
    title, dur, upl = rows[-1].split("\t")
    return {
        "title": title.strip()[:120],
        "duration": int(float(dur)) if dur.strip().replace(".", "").isdigit() else 0,
        "uploader": upl.strip()[:60],
    }


def start_height(duration, want):
    """Where the ladder starts: an explicit request wins, otherwise scale with length
    so a 2-hour video does not begin by pulling 4K."""
    if want:
        return int(want)
    if duration <= 600:
        return 2160
    if duration <= 1200:
        return 1440
    if duration <= 2700:
        return 1080
    if duration <= 7200:
        return 720
    return 480


def fetch_video(url, workdir, top, max_mb):
    """Best mp4 whose *actual* size fits max_mb. Returns (path, height) or (None, 0).

    No --max-filesize: yt-dlp then rejects whole ladders ("Requested format is not
    available") when a size estimate is missing, which silently dropped us to 360p.
    Download, measure, step down — the wasted megabytes are cheaper than a broken build.
    """
    for h in [x for x in LADDER if x <= top]:
        fmt = (
            f"bv*[height<={h}][vcodec^=avc1]+ba[acodec^=mp4a]/"
            f"b[height<={h}][vcodec^=avc1]/bv*[height<={h}]+ba/b[height<={h}]"
        )
        out = os.path.join(workdir, f"v{h}.mp4")
        rc, _ = sh([
            "yt-dlp", "--no-playlist", "--no-warnings", "-f", fmt,
            "--merge-output-format", "mp4",
            "--ppa", "Merger+ffmpeg_o:-movflags +faststart",
            "-o", out, url,
        ])
        if rc == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            if os.path.getsize(out) <= max_mb * 1048576:
                return out, h
            C.log(f"  {h}p = {os.path.getsize(out) / 1048576:.1f} MB > {max_mb} MB, stepping down")
            os.remove(out)
    return None, 0


def fetch_audio(url, workdir):
    out = os.path.join(workdir, "song.mp3")
    rc, _ = sh([
        "yt-dlp", "--no-playlist", "--no-warnings", "-x",
        "--audio-format", "mp3", "--audio-quality", "128K",
        "--embed-metadata", "-o", out, url,
    ])
    if rc == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    return None


def fetch_audio_from_file(src, workdir):
    """YouTube blocks yt-dlp on runners ("confirm you're not a bot"), but the resolver's mp4 proxy
    is fast — take the bytes we can get and let ffmpeg drop the video track."""
    raw = os.path.join(workdir, "src.bin")
    try:
        with requests.get(src, stream=True, timeout=600, headers={"user-agent": TG_UA}) as r:
            r.raise_for_status()
            with open(raw, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    if chunk:
                        fh.write(chunk)
    except Exception as e:  # noqa: BLE001
        C.log(f"  src download failed: {e}")
        return None
    out = os.path.join(workdir, "song.mp3")
    rc, _ = sh(["ffmpeg", "-y", "-i", raw, "-vn", "-b:a", "128k", "-metadata", "title= " + "", out], timeout=900)
    if rc == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    return None


def up_sendit(path, fname):
    """PUT style: the file name lives in the URL path, so the host serves it back
    with the right content-type and content-disposition."""
    with open(path, "rb") as fh:
        r = requests.put("https://sendit.sh/" + fname, data=fh, timeout=1800,
                         headers={"user-agent": C.UA,
                                  "content-type": "application/octet-stream"})
    r.raise_for_status()
    m = re.search(r"https://sendit\.sh/\S+", r.text or "")
    if not m:
        raise RuntimeError(f"sendit.sh no url: HTTP {r.status_code} {(r.text or '')[:160]}")
    return m.group(0).strip()


def up_tempsh(path, fname):
    with open(path, "rb") as fh:
        r = requests.post("https://temp.sh/upload", timeout=1800,
                          files={"file": (fname, fh)},
                          headers={"user-agent": C.UA})
    r.raise_for_status()
    url = (r.text or "").strip().splitlines()[0].strip() if r.text.strip() else ""
    if not url.startswith("http"):
        raise RuntimeError(f"temp.sh no url: HTTP {r.status_code} {(r.text or '')[:160]}")
    return url


def up_x0(path, fname):
    with open(path, "rb") as fh:
        r = requests.post("https://x0.at/", timeout=1800,
                      files={"file": (fname, fh)},
                      data={"keep_name": "1"},
                      headers={"user-agent": C.UA})
    r.raise_for_status()
    url = (r.text or "").strip().splitlines()[0].strip() if r.text.strip() else ""
    if not url.startswith("http"):
        raise RuntimeError(f"x0.at no url: HTTP {r.status_code} {(r.text or '')[:160]}")
    C.log(f"  x0.at expires: {r.headers.get('x-expires') or r.headers.get('expires') or '?'}")
    return url


def up_kappa(path, fname):
    with open(path, "rb") as fh:
        r = requests.post("https://kappa.lol/api/upload", timeout=1800,
                      files={"file": (fname, fh)},
                      headers={"user-agent": C.UA})
    r.raise_for_status()
    j = r.json()
    url = None
    if isinstance(j, dict):
        url = ((j.get("files") or [{}])[0] or {}).get("url") or j.get("link")
    if not url:
        raise RuntimeError(f"kappa no url: {str(j)[:160]}")
    return url


UPLOADERS = {
    "sendit.sh": up_sendit,
    "temp.sh": up_tempsh,
    "x0.at": up_x0,
    "kappa.lol": up_kappa,
}


def upload(path, fname):
    """First host that takes the file, temporary ones first. Returns (url, host, expiry)."""
    mb = os.path.getsize(path) / 1048576
    for name, limit_mb, exp in HOSTS:
        if mb > limit_mb:
            C.log(f"  skip {name}: {mb:.1f} MB > {limit_mb} MB limit")
            continue
        try:
            url = UPLOADERS[name](path, fname)
            if url:
                C.log(f"  upload {name} -> {url}")
                return url, name, exp
        except Exception as e:  # noqa: BLE001
            C.log(f"  upload {name} failed: {e}")
    return None, "", ""


def check(url):
    """A link that serves HTML or 4xx is useless as a direct download."""
    try:
        r = requests.get(url, timeout=120, headers={"user-agent": TG_UA}, stream=True)
        ct = (r.headers.get("content-type") or "").lower()
        first = next(r.iter_content(4096), b"")
        C.log(f"  fetch {r.status_code} {ct} {len(first)}b")
        if r.status_code != 200 or "text/html" in ct or not first:
            C.log("  WARN: link does not look like a direct download")
    except Exception as e:  # noqa: BLE001
        C.log(f"  fetch failed: {e}")


def post(cb, payload):
    if not cb:
        C.log("callback: " + json.dumps(payload, ensure_ascii=False))
        return
    try:
        r = requests.post(cb, json=payload, timeout=60)
        C.log(f"callback {r.status_code} {r.text[:200]}")
    except Exception as e:  # noqa: BLE001
        C.log(f"callback failed: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--kind", default="best")
    ap.add_argument("--max-mb", type=int, default=DEFAULT_MAX_MB)
    ap.add_argument("--cb", default="")
    ap.add_argument("--tok", default="")
    ap.add_argument("--src", default="", help="direct media URL: skip yt-dlp and transcode this instead")
    a = ap.parse_args()

    kind = a.kind if a.kind in ("mp3", "best") or a.kind.isdigit() else "best"
    info = probe(a.url)
    C.log(f"meta: {json.dumps(info, ensure_ascii=False)}")
    title = info.get("title") or "video"
    duration = info.get("duration") or 0

    with tempfile.TemporaryDirectory() as td:
        if kind == "mp3":
            path = fetch_audio_from_file(a.src, td) if a.src else fetch_audio(a.url, td)
            fname = "audio.mp3"
            fmt = "MP3"
        else:
            top = start_height(duration, None if kind == "best" else kind)
            path, h = fetch_video(a.url, td, top, a.max_mb)
            fname = "video.mp4"
            fmt = f"{h}p" if h else ""
        if not path:
            post(a.cb, {"tok": a.tok, "err": f"download failed (private / age-restricted / larger than {a.max_mb} MB)"})
            sys.exit(1)
        size = os.path.getsize(path)
        C.log(f"result: {path} {size / 1048576:.2f} MB")
        url, host, exp = upload(path, fname)
        if not url:
            post(a.cb, {"tok": a.tok, "err": "upload failed on every host"})
            sys.exit(1)
        C.log(f"UPLOAD {host} -> {url}")
        check(url)
        post(a.cb, {
            "tok": a.tok, "url": url, "kind": kind, "fmt": fmt, "exp": exp,
            "title": title, "duration": duration, "size": size, "host": host,
        })


if __name__ == "__main__":
    main()
