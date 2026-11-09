#!/usr/bin/env python3
"""kibo-builder common — shared plumbing for the build jobs: download with mirror
fallback, image re-encode, uploads to raw file hosts, and the worker callback.

Used by build_gallery.py and build_video.py. Keep this file dependency-light:
requests + pillow only (img2pdf is imported lazily by the gallery builder).
"""
import io
import re
import time

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Mirror rotation for the gallery CDN — one host can 404/rate-limit, others keep serving.
# Derived from the URL itself (numbered iN subdomains + bare domain), no site names here.


def log(*a):
    print(*a, flush=True)


def alt_urls(url):
    """Same asset on every CDN mirror (original host still tried first)."""
    m = re.match(r"https?://([^/]+)(/.*)$", url)
    if not m:
        return [url]
    host, path = m.group(1), m.group(2)
    parts = host.split(".")
    if len(parts) >= 3 and re.fullmatch(r"i\d?", parts[0]):
        domain = ".".join(parts[1:])
        mirrors = [f"i{n}.{domain}" for n in range(1, 5)] + [domain]
        return [url] + [f"https://{h}{path}" for h in mirrors if h != host]
    return [url]


def referer_of(url):
    """Site root of an asset URL — the CDN checks it."""
    m = re.match(r"https?://([^/]+)", url)
    if not m:
        return url
    parts = m.group(1).split(".")
    root = ".".join(parts[1:]) if len(parts) >= 3 and re.fullmatch(r"i\d?", parts[0]) else m.group(1)
    return f"https://{root}/"


def download_page(url, rounds=2):
    """Fetch one page; try every mirror, 2 rounds. None = give up."""
    urls = alt_urls(url)
    last = "?"
    for rnd in range(rounds):
        for u in urls:
            try:
                r = requests.get(u, timeout=60, headers={
                    "user-agent": UA, "referer": referer_of(url)})
                if r.status_code < 400 and len(r.content) > 0:
                    return r.content
                last = f"HTTP {r.status_code}"
            except Exception as e:  # noqa: BLE001
                last = type(e).__name__
            time.sleep(0.15)
        time.sleep(0.6 * (rnd + 1))
    log(f"  page failed: {url[-50:]} ({last})")
    return None


def ext_of(url):
    m = re.search(r"\.(jpg|jpeg|png|gif|webp)(?:\?|$)", url, re.I)
    return (m.group(1).lower() if m else "jpg").replace("jpeg", "jpg")


def to_jpeg(data, max_w, q):
    """img2pdf embeds webp as lossless PNG (100 KB page -> 1.3 MB pdf page).
    Re-encode to JPEG first so the pdf stays sane."""
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    if im.mode != "RGB":
        im = im.convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)),
                       Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=q, optimize=True, progressive=True)
    return out.getvalue()


def upload_x0(buf, fname):
    """x0.at — raw, auto-expire, 1 GiB cap. Keeps the filename with keep_name=1."""
    r = requests.post("https://x0.at/", timeout=300,
                      files={"file": (fname, buf)},
                      data={"keep_name": "1"},
                      headers={"user-agent": UA})
    r.raise_for_status()
    url = (r.text or "").strip().splitlines()[0].strip() if r.text.strip() else ""
    if not url.startswith("http"):
        raise RuntimeError(f"x0.at failed: HTTP {r.status_code} {r.text[:120]}")
    log(f"x0.at expires: {r.headers.get('x-expires') or r.headers.get('expires') or '?'}")
    return url


def upload_kappa(buf, fname):
    r = requests.post("https://kappa.lol/api/upload", timeout=180,
                      files={"file": (fname, buf)},
                      headers={"user-agent": UA})
    r.raise_for_status()
    j = r.json()
    url = None
    if isinstance(j, dict):
        url = ((j.get("files") or [{}])[0] or {}).get("url") or j.get("link")
    if not url:
        raise RuntimeError(f"kappa failed: {str(j)[:120]}")
    return url


UPLOADERS = {"x0.at": upload_x0, "kappa.lol": upload_kappa}

# Both hosts in this chain serve RAW bytes (the bot's sendDocument needs the
# actual file, not a viewer page). Viewer-page hosts are useless here.
HOST_ORDER = {
    "auto": ["kappa.lol", "x0.at"],
    "x0at": ["x0.at", "kappa.lol"],
    "kappa": ["kappa.lol", "x0.at"],
}


def upload(buf, fname, host="auto"):
    order = HOST_ORDER.get(host) or HOST_ORDER["auto"]
    for name in order:
        try:
            url = UPLOADERS[name](buf, fname)
            log(f"upload {name} -> {url}")
            return url, name
        except Exception as e:  # noqa: BLE001
            log(f"upload {name} failed: {e}")
    raise RuntimeError("all upload hosts failed")


def callback(cb, tok, payload, tries=3):
    """Report the result back to the bot worker (one-time job key)."""
    for i in range(tries):
        try:
            r = requests.post(cb, timeout=30, json=dict(tok=tok, **payload),
                              headers={"user-agent": "kibo-builder"})
            if r.status_code < 500:
                log(f"callback {r.status_code}")
                return
        except Exception as e:  # noqa: BLE001
            log(f"callback try {i + 1} failed: {e}")
        time.sleep(2 * (i + 1))
    log("callback could not be delivered (retries exhausted)")
