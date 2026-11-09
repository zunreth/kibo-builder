#!/usr/bin/env python3
"""kibo-builder — gallery packager: zip / pdf / one-long-image via GitHub Actions.

Triggered by repository_dispatch (event type "nh") from a kibo bot instance:
the bot sends {id, fmt, meta, cb, tok, host, pdfq}; this job downloads the pages
through the metadata endpoint, packages them, uploads the file to a raw host and
calls the bot back with the link. No secrets needed in this repo — the callback
URL and one-time token arrive per job.
"""
import argparse
import io
import os
import sys
import tempfile
import zipfile

import requests
from PIL import Image

import common as C

SCROLL_MAX_W = 900
SCROLL_MAX_H = 24000
SCROLL_MAX_PAGES = 60
PDF_MAX_W = 1600
PDF_Q = 82
PDF_PRESETS = {"normal": (PDF_MAX_W, PDF_Q), "small": (1100, 76)}


def meta_pages(base, gid):
    r = requests.get(f"{base}/api/gallery/{gid}", timeout=45,
                     headers={"user-agent": C.UA})
    r.raise_for_status()
    j = r.json()
    pages = (j or {}).get("pages") or []
    if not pages or not isinstance(pages[0], dict) or not pages[0].get("url"):
        raise RuntimeError(f"metadata has no valid pages ({len(pages)} rows)")
    return j


def build_zip(items, gid):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for n, data, ext in items:
            z.writestr(f"{n:03}.{ext}", data)
    return f"{gid}.zip", out.getvalue()


def build_pdf(items, gid, max_w=PDF_MAX_W, q=PDF_Q):
    import img2pdf
    tmp = tempfile.mkdtemp(prefix="kibopdf-")
    paths = []
    for n, data, _ext in items:
        p = os.path.join(tmp, f"{n:03}.jpg")
        with open(p, "wb") as f:
            f.write(C.to_jpeg(data, max_w, q))
        paths.append(p)
    with open(os.path.join(tmp, "out.pdf"), "wb") as f:
        f.write(img2pdf.convert(paths))
    with open(os.path.join(tmp, "out.pdf"), "rb") as f:
        return f"{gid}.pdf", f.read()


def build_scroll(items, gid):
    use = items[:SCROLL_MAX_PAGES]
    imgs = []
    for _n, data, _ext in use:
        im = Image.open(io.BytesIO(data))
        imgs.append(im if im.mode == "RGB" else im.convert("RGB"))
    if not imgs:
        raise RuntimeError("no pages to stitch")
    total = sum(round(im.height * SCROLL_MAX_W / im.width) for im in imgs)
    width = SCROLL_MAX_W if total <= SCROLL_MAX_H else max(
        320, int(SCROLL_MAX_W * SCROLL_MAX_H / total))
    height = sum(round(im.height * width / im.width) for im in imgs)
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    y = 0
    for im in imgs:
        h = round(im.height * width / im.width)
        canvas.paste(im.resize((width, h), Image.LANCZOS), (0, y))
        y += h
    out = io.BytesIO()
    # JPEG not PNG: an 807x24000 photo canvas is ~14 MB as PNG vs ~3.5 MB at q85.
    canvas.save(out, "JPEG", quality=85, optimize=True, progressive=True)
    return f"{gid}_scroll.jpg", out.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--fmt", default="zip")
    ap.add_argument("--host", default="auto")
    ap.add_argument("--pdfq", default="normal", choices=sorted(PDF_PRESETS))
    ap.add_argument("--meta", required=True, help="metadata endpoint base URL")
    ap.add_argument("--cb", default="")
    ap.add_argument("--tok", default="")
    ap.add_argument("--no-cb", action="store_true", help="dry run: no callback")
    a = ap.parse_args()

    gid = int(a.id)
    fmt = a.fmt if a.fmt in ("zip", "pdf", "scroll") else "zip"
    try:
        log(f"fetch meta {gid}")
        meta = meta_pages(a.meta, gid)
        title = str(meta.get("title") or f"gallery #{gid}")[:110]
        total = meta.get("num_pages") or len(meta["pages"])
        pages = meta["pages"]
        if total and len(pages) > total:
            pages = pages[:total]

        log(f"download {len(pages)} pages")
        items, miss = [], 0
        for i, p in enumerate(pages, 1):
            data = C.download_page(p["url"])
            if data:
                items.append((i, data, C.ext_of(p["url"])))
            else:
                miss += 1
            if i % 20 == 0:
                log(f"  {i}/{len(pages)}")
        if not items:
            raise RuntimeError("every page failed to download")
        if miss > max(2, len(pages) // 4):
            raise RuntimeError(f"{miss}/{len(pages)} pages failed to download")

        if fmt == "pdf":
            pw, pq = PDF_PRESETS.get(a.pdfq) or PDF_PRESETS["normal"]
            fname, buf = build_pdf(items, gid, pw, pq)
        elif fmt == "scroll":
            fname, buf = build_scroll(items, gid)
        else:
            fname, buf = build_zip(items, gid)
        log(f"built {fname} ({len(buf) / 1048576:.1f} MB)")

        warn = ""
        if fmt == "scroll" and len(items) > SCROLL_MAX_PAGES:
            warn = f"scroll {SCROLL_MAX_PAGES}/{len(items)} pages"
        if miss:
            warn = (warn + " · " if warn else "") + f"{miss} pages failed"

        url, hostname = C.upload(buf, fname, a.host)
        if a.no_cb:
            log("DONE (dry run, callback skipped)")
            return
        # host + size ride along: the worker uses host for cache TTL, size for stats
        C.callback(a.cb, a.tok, {"url": url, "id": str(gid), "fmt": fmt,
                                 "title": title, "pages": len(items),
                                 "host": hostname, "size": len(buf),
                                 "warn": warn})
        log("DONE")
    except Exception as e:  # noqa: BLE001
        log(f"FAILED: {e}")
        if not a.no_cb:
            C.callback(a.cb, a.tok, {"err": str(e)[:300], "id": str(gid),
                                      "fmt": a.fmt})
        sys.exit(1)


if __name__ == "__main__":
    main()
