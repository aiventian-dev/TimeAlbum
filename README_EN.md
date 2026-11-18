# Time Album

**Read-only local photo album** that runs on your own machine. It reads a macOS Photos library (`.photoslibrary`) or any folder of photos and lets you browse them as **year → month → continuous photo stream**. The older a photo is, the more it fades, yellows and grains — time itself is the filter.

Nothing is copied, moved or modified. The server only reads; generated thumbnails live in the local `cache/` directory.

[中文说明](README.md)

## Screenshots

| | |
|---|---|
| ![1](docs/poster/01.png) | ![2](docs/poster/02.png) |
| ![3](docs/poster/03.png) | ![4](docs/poster/04.png) |
| ![5](docs/poster/05.png) | ![6](docs/poster/06.png) |

## Features

* **Read-only** — never writes, deletes or alters originals, and needs no cloud account
* **Age-based aging** — fade, grain and film color scale automatically with how long ago the photo was taken
* **Film simulation** — seven stocks (NC, Portra, Gold, Superia, Velvia, HP5, 800T) with per-stock strength, plus an independent film-grain layer
* **Aging panel** — one slider drives both time fade and the year-appropriate film color, and stacks on any film stock; brand marks can be shown in mono or color
* **Three thumbnail sizes** — `s` / `m` / `l`, picked automatically by grid density
* **Multiple libraries** — mount Photos libraries and plain folders side by side; rescan, add or remove at any time
* **Smooth zoom** — mouse wheel, trackpad pinch and mobile pinch; double-tap to fill the screen, and rotate to straighten
* **State you can come back to** — a reload keeps you on the same photo
* **Film changes stay temporary in the viewer** — switching stock in the detail view only affects the current session and is cleared on exit

## Requirements

macOS and Python 3 (the system Python is enough; no third-party packages). Thumbnail generation uses the built-in `sips` tool, so macOS is required today — Windows is not supported yet.

## Run

```bash
git clone <this repo>
cd TimeAlbum
cp libraries.example.json libraries.json   # then point it at your own libraries
python3 album_server.py                    # listens on port 8905 by default
```

You can also double-click `启动相册.command`, which opens [http://127.0.0.1:8905](http://127.0.0.1:8905) in your browser.

```bash
python3 album_server.py --port 9000
python3 album_server.py --lib "/Users/you/Pictures/照片图库.photoslibrary"
```

If the port is taken, the server moves on to 8906, 8907 and so on.

On first start it scans `/Volumes/` for `.photoslibrary` bundles. For libraries elsewhere you do not have to edit files: open **Library management** in the top bar and paste a path or folder; changes apply without restarting.

### Permissions

Reading a Photos library is protected by macOS. If libraries show as greyed out or the page says the database cannot be read, go to `System Settings → Privacy & Security → Full Disk Access`, add Terminal (or whichever app you launched), enable it, then restart the server.

The first `python3` run may prompt you to install the Command Line Tools — accept once.

## Password (optional)

Authentication is **off by default**. To require a password, create `密码.txt` in this directory and put the password on its first line — no code changes needed. Or use an environment variable:

```bash
ALBUM_PASSWORD=my-pass python3 album_server.py
```

`密码.txt` is listed in `.gitignore` and will not be committed. Turning auth off and confirming `.gitignore` before taking screenshots or recordings is recommended.

## Layout

```
album_server.py        # single-file server (stdlib only, includes thumbnail generation and caching)
时光相册.html          # single-page front end (inline CSS/JS; refresh to pick up edits)
启动相册.command       # double-click launcher for macOS
libraries.example.json # library config sample (copy to libraries.json)
品牌标识/              # camera and phone brand SVGs used for EXIF badges
docs/poster/           # intro poster pages
cache/                 # thumbnail cache (generated, not tracked)
```

## Notes

* Videos are skipped; only photos are indexed.
* Everything is served on localhost. Do not expose the port directly to the internet — add your own reverse proxy and authentication if you need remote access.
* Logos in `品牌标识/` belong to their respective brands and are used only to label camera EXIF data. No affiliation or endorsement is implied.

## License

MIT — see [LICENSE](LICENSE).
