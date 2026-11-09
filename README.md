# kibo-builder — the optional GitHub Actions add-on for [kibo](https://github.com/ruqes/kibo)

A tiny build service for kibo: two workflows that render files a Cloudflare Worker can't build itself —
**gallery packages** (zip / pdf / one-long-image) and **video/audio files** (any quality, via yt-dlp) —
upload the result to a raw file host, and call your bot back with the link.

Think of it as a plugin: kibo works fully without it (albums, resolvers and small files never leave
the Worker). This repo only adds the "big file / packaged file" tier, running on **your own** GitHub
account, consuming **your own** Actions minutes.

## How it works

```
kibo (Cloudflare Worker)                     kibo-builder (GitHub Actions, yours)
        │  repository_dispatch  ────────────────────▶  workflow run
        │        {id|url, fmt, meta, cb, tok}            download → build → upload
        │                                                  (x0.at / kappa.lol / sendit.sh / temp.sh)
        │  POST result {url, tok, ...}  ◀────────────  callback to your worker
```

- No secrets live in this repo. Every job receives its own callback URL + one-time token from your bot.
- Build results land on temporary raw hosts; your bot fetches them into the chat and caches the link.

## Install

1. Create a repo from this one (Use this template → Create repository, or fork it).
2. Make a GitHub PAT with the **`workflow`** scope (classic PAT, or fine-grained with
   "Actions: write" on this repo).
3. Give the token to your kibo instance as the `GH_DISPATCH` secret, and point `NHBUILD_REPO`
   at your copy (`yourname/kibo-builder`).
4. Send a build command to your bot — it dispatches here and posts back the file when the job finishes.

## Repository dispatch events

| Event type | Payload | Does |
|---|---|---|
| `nh` | `id, fmt (zip\|pdf\|scroll), meta, host (auto\|kappa\|x0at), pdfq (normal\|small), cb, tok` | downloads the gallery pages through the metadata endpoint, packages them, uploads, callbacks |
| `yt` | `url, kind (best\|<height>\|mp3), max_mb, src?, cb, tok` | renders the video/audio with yt-dlp, uploads, callbacks |

## Local dry run

```bash
pip install requests pillow img2pdf yt-dlp ffmpeg   # plus ffmpeg from your package manager
python build_gallery.py --id 177013 --fmt pdf --meta <your metadata base> --no-cb
python build_video.py --url "https://youtu.be/..." --kind 720 --no-cb
```

## Notes

- The gallery metadata endpoint (`meta` payload field) is served by the kibo bot itself —
  self-hosters get one automatically with their worker.
- GitHub's runners are blocked by some platforms now and then; a job can fail for reasons outside
  this repo's control. The bot reports failures honestly and you can retry.
- Hosts chain: `auto` prefers permanent links, with x0.at as fallback; big videos go to the
  temporary hosts first (sendit.sh 10 GB / temp.sh 4 GB / x0.at 1 GB / kappa.lol 100 MB).

## License

MIT — same as kibo.
