# Karaoke Tool

A local karaoke system that downloads a song, strips vocals with Demucs, and lets you sing along — seamlessly switching between the **original mix** and the **instrumental** at any moment without losing playback position.

Comes with both a **web GUI** (recommended) and a **CLI player**.

---

## Features

- **AI vocal separation** — uses [Demucs](https://github.com/facebookresearch/demucs) (`htdemucs` model) offline to produce studio-quality instrumentals
- **Smart YouTube Music search** — type a song title + artist, get multiple candidates with cover art / duration; the best match auto-starts downloading
- **Direct URL support** — paste a YouTube video link or Spotify track link and it downloads immediately
- **Playlist import** — paste a Spotify, YouTube, or Bilibili playlist URL to batch-download and separate every track
- **Local file separation** — drag in any audio file (mp3, wav, flac, …) to separate it without internet access
- **Synced scrolling lyrics** — fetches LRC lyrics automatically; the web UI highlights the current line as the song plays
- **Seamless track switching** — original ⇄ instrumental toggle is sample-accurate with no gap, stutter, or progress reset
- **Content-hash caching** — the same audio file is never separated twice; cache hits open in under 0.1 s
- **Instrumental export** — every separated track is mirrored to a `伴奏/` folder (flat, human-readable filenames) and can be downloaded directly from the browser
- **Sound card selection** — choose the output audio device from the web UI

---

## Requirements

| Dependency | Purpose |
|---|---|
| Python 3.10+ | Runtime |
| `ffmpeg` | Audio decoding / encoding (system package) |
| `demucs` | Vocal separation model |
| `yt-dlp` | YouTube audio download |
| `ytmusicapi` | YouTube Music search |
| `spotdl` | Spotify track resolution via YouTube Music |
| `sounddevice` / `soundfile` / `numpy` | Audio playback |
| `fastapi` + `uvicorn` | Web GUI server |
| `syncedlyrics` | LRC lyrics fetching |
| `deno` *(optional)* | Stabilises yt-dlp against YouTube JS challenges |

Install system packages on macOS:

```bash
brew install ffmpeg          # required
brew install deno            # optional but recommended (fixes occasional HTTP 403)
```

---

## Installation

```bash
git clone <this-repo>
cd karaoke
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

> **Apple Silicon note:** Demucs runs on the MPS GPU by default (`--device mps`). The first run downloads the `htdemucs` model (~80 MB).

---

## Quick Start

### Web GUI (recommended)

```bash
./venv/bin/python karaoke.py --gui
```

Open [http://localhost:8000](http://localhost:8000) in your browser. Audio plays through your computer's speakers; the browser is the control panel.

### CLI player

```bash
# Local file
./venv/bin/python karaoke.py ~/Music/song.mp3

# YouTube link
./venv/bin/python karaoke.py "https://www.youtube.com/watch?v=XXXX"

# Spotify track
./venv/bin/python karaoke.py "https://open.spotify.com/track/XXXX"
```

---

## CLI Options

| Flag | Default | Description |
|---|---|---|
| `source` | — | Local file path, YouTube URL, or Spotify URL. Optional when `--gui` is used. |
| `--gui` | off | Open the web application instead of the terminal player |
| `--device mps\|cpu\|cuda` | `mps` | Demucs compute device. Falls back to CPU automatically if MPS is unavailable. |
| `--no-cache` | off | Force re-separation even if a cached result exists |

---

## Web GUI Walkthrough

### Search & Download
Type a song title (and optionally artist name) into the search box. The app queries YouTube Music and returns several candidates with cover art, artist, and duration. The closest match to the original studio recording is marked **★ auto** and starts downloading immediately. You can stop it and click a different candidate if needed.

Alternatively, paste a YouTube video URL directly — it skips the search step and downloads at once.

### Local File Separation
Click **📁 Choose local file** to pick any audio file on disk. Separation runs offline; no internet needed.

### Playlist Import
Paste a Spotify, YouTube, or Bilibili playlist URL and click **📜 Import playlist**. Each track is queued, downloaded, and separated in sequence. A progress list shows per-track status; **■ Stop all** cancels the remaining queue.

### Library
All processed songs appear in **My Library**. Click a title to load and play it instantly. Each row shows whether synced lyrics are available and offers **⬇ Instrumental** / **⬇ Original** download buttons to save the separated WAV files.

### Playback Controls
- Play / Pause
- Seek bar with click-to-seek and drag
- **Original ⇄ Instrumental** toggle (seamless, no gap)
- Sound card selector
- Synced lyrics panel — current line is highlighted and scrolls automatically

> Internet is required for: search, YouTube/Spotify download, playlist import, and lyrics fetch.  
> Everything else (local separation, library, playback, export) works fully offline.

---

## CLI Keyboard Shortcuts

Available when running without `--gui`:

| Key | Action |
|---|---|
| `v` | Toggle original ⇄ instrumental |
| `Space` | Pause / Resume |
| `←` / `→` | Seek backward / forward 5 s |
| `q` | Quit |

---

## How It Works

```
Source (local file / YouTube URL / Spotify URL)
    │
    ▼
acquire.py ──── yt-dlp / spotdl → downloads/  (mp3 320k)
    │
    ▼
separate.py ─── Demucs htdemucs model
    │               └─ cache/<content-hash>/<song>/
    │                       ├─ vocals.wav
    │                       └─ no_vocals.wav
    │
    ├─► player.py ────── sounddevice callback
    │       Both tracks loaded into RAM; toggle swaps
    │       the output buffer without touching position.
    │
    └─► webgui.py ────── FastAPI + single-page app
            Background threads: download → separate → cache
            Frontend polls /job/<id> for progress
```

### Module Reference

| File | Responsibility |
|---|---|
| `karaoke.py` | CLI entry point, argument parsing |
| `acquire.py` | Audio acquisition: local passthrough, YouTube download (`yt-dlp`), Spotify resolution (`spotdl`), YouTube Music search (`ytmusicapi`), playlist listing |
| `separate.py` | Demucs separation with content-hash caching; mirrors instrumentals to `伴奏/` |
| `library.py` | Scans `cache/` to enumerate processed songs; deduplication helpers |
| `player.py` | `KaraokePlayer` class — loads both stems into memory, `sounddevice` stream callback, seek/pause/toggle |
| `webgui.py` | FastAPI web server; REST API for library, playback, search, download jobs, batch imports |
| `lyrics.py` | Fetches LRC lyrics via `syncedlyrics`, stores in `cache/<id>/lyrics.lrc`, parses into `[{t, text}]` |

---

## Directory Layout

```
karaoke/
├── karaoke.py          # CLI entry point
├── acquire.py          # Audio acquisition
├── separate.py         # Demucs separation + cache
├── library.py          # Song library
├── player.py           # Playback engine
├── webgui.py           # Web GUI server
├── lyrics.py           # Synced lyrics
├── requirements.txt    # Python dependencies
├── cache/              # Separated stems (keyed by audio content hash)
│   └── <hash>/
│       └── <song name>/
│           ├── vocals.wav
│           └── no_vocals.wav
├── downloads/          # Raw downloaded audio (mp3)
└── 伴奏/               # Flat mirror of all instrumentals for easy access
```

---

## Performance

| Device | Separation time (3-min stereo track) |
|---|---|
| MPS (Apple Silicon GPU) | ~44 s |
| CPU | 12+ min |
| Cache hit (same file again) | < 0.1 s |

*Benchmark: Benson Boone — Beautiful Things, 192 s stereo. First run also downloads the ~80 MB model.*

---

## Troubleshooting

### Occasional HTTP 403 / "No JS runtime" warning
YouTube increasingly requires solving a JavaScript challenge. Without a JS runtime, some downloads fail with `HTTP Error 403`. Install Deno to eliminate this:

```bash
brew install deno
```

yt-dlp detects and uses it automatically. Without Deno, retrying or picking an alternate search candidate usually works.

### Slow first search / download
The app forces IPv4 connections at startup to avoid a ~120 s timeout when the local IPv6 stack has no external connectivity. This is automatic and transparent.

### MPS not available
If you see a MPS-related error, pass `--device cpu` explicitly. Separation will be slower but still correct.

---

## Roadmap

- Microphone mix-in and recording playback
- Real-time streaming mode (capture system audio → rolling-buffer separation)
- Pitch shifting / key change control
