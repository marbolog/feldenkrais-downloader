# Feldenkrais Project Downloader

Scripts that find and download all publicly available (free) audio lessons from [feldenkraisproject.com](https://feldenkraisproject.com/). Files are saved locally and optionally uploaded to Google Drive.

## Background: what's actually downloadable

The site has 175 lessons. **67 are free** and **108 are patron-only**:

| Player type | Lessons | Auth required | Strategy |
|---|---|---|---|
| **Podlove Web Player** | ~67 | No | Audio URL embedded in raw HTML as server-side JSON — extractable with regex |
| **Bumper Player** | ~108 | Yes (Restrict Content Pro) | Player shortcode blocked for anonymous visitors — no audio in HTML or network traffic |

The patron-only lessons cannot be downloaded without authentication credentials regardless of the tool used (HTTP, Playwright, or any other approach).

## Recommended script: `download_from_sitemap.py`

Reads the Yoast SEO lesson sitemap directly — 175 known URLs, no BFS crawl needed. Scans each page for a Podlove audio URL, downloads all free lessons, and optionally uploads to Google Drive.

```bash
# Download everything (skips already-downloaded files)
uv run python download_from_sitemap.py

# Specify a different output directory
uv run python download_from_sitemap.py --output-dir /path/to/audio

# Override the Google Drive folder (default: 1DoeAFcPcKXxw25bwXyaooTAWA0BfFLy0)
uv run python download_from_sitemap.py --gdrive-folder-id YOUR_FOLDER_ID

# Tune concurrency and rate-limiting
uv run python download_from_sitemap.py --workers 8 --download-delay 0.25

# Sync already-downloaded files to Google Drive (no re-download)
uv run python download_from_sitemap.py --sync-only
```

| Flag | Default | Description |
|---|---|---|
| `--output-dir` | `downloads` | Local directory for audio files |
| `--gdrive-folder-id` | `1DoeAFcPcKXxw25bwXyaooTAWA0BfFLy0` | Google Drive folder ID for upload |
| `--workers` | `4` | Parallel download threads |
| `--download-delay` | `0.5` | Seconds between downloads |
| `--log-level` | `INFO` | Verbosity (`DEBUG`, `INFO`, `WARNING`) |
| `--sync-only` | off | Skip download; upload all local files to Drive |
| `--auth-port` | `9090` | Local port for the OAuth callback server |

Typical runtime: ~2 minutes for 175 page fetches + download time.

## Other scripts

### `feldenkrais_downloader.py` — BFS HTTP crawler

Full-site BFS crawl using `requests` + `BeautifulSoup`. Discovers audio URLs from tags and inline scripts. Not recommended for this site: wastes time crawling 1,400+ non-lesson pages (pagination, categories, query-string variants).

```bash
uv run python feldenkrais_downloader.py
uv run python feldenkrais_downloader.py --path-substring-filter /lesson/ --gdrive-folder-id YOUR_FOLDER_ID
```

| Flag | Default | Description |
|---|---|---|
| `--start-url` | Homepage | Starting URL |
| `--output-dir` | `downloads` | Local directory |
| `--max-pages` | *(no limit)* | Stop after N pages |
| `--crawl-delay` | `1.0` | Seconds between page fetches |
| `--download-delay` | `1.0` | Seconds between file downloads |
| `--path-substring-filter` | *(none)* | Only enqueue URLs containing this string |
| `--gdrive-folder-id` | *(none)* | Google Drive folder ID |
| `--log-level` | `INFO` | Verbosity |
| `--workers` | `4` | Parallel download threads |

### `feldenkrais_downloader_playwright.py` — Headless Chromium

Opens each lesson page in a headless browser and captures audio URLs from network traffic and the DOM. Useful if the Podlove regex ever breaks. Cannot bypass the patron-only Bumper player (RCP blocks the player from rendering entirely for anonymous users, so there is nothing to capture).

```bash
uv run python feldenkrais_downloader_playwright.py
uv run python feldenkrais_downloader_playwright.py --single-lesson-url https://feldenkraisproject.com/lesson/some-lesson/
```

| Flag | Default | Description |
|---|---|---|
| `--start-url` | Homepage | Starting URL for link discovery |
| `--output-dir` | `downloads` | Local directory |
| `--max-pages` | *(no limit)* | Stop crawling after N pages |
| `--network-idle-timeout-ms` | `8000` | Extra wait after page load |
| `--navigation-timeout-ms` | `60000` | Max page navigation time |
| `--single-lesson-url` | *(none)* | Skip crawling; process one URL |
| `--gdrive-folder-id` | *(none)* | Google Drive folder ID |
| `--log-level` | `INFO` | Verbosity |
| `--workers` | `4` | Parallel download threads |

## Requirements

```bash
uv sync
uv run playwright install chromium   # only for feldenkrais_downloader_playwright.py
```

Python 3.10+ required. Install `uv` with `curl -LsSf https://astral.sh/uv/install.sh | sh`.

For Google Drive upload you need a `credentials.json` OAuth2 **Desktop app** client secrets file in the working directory ([GCP Console](https://console.cloud.google.com/) → APIs & Services → Credentials → Create OAuth 2.0 Client ID → Desktop app). `token.json` is created automatically on first auth and reused on subsequent runs.

## Output

Audio files are written to `--output-dir` (default: `downloads/`). Each filename includes a 6-character SHA256 hash suffix for collision safety.

```
downloads/
  010_Intro_to_Lesson_1_36b47f.mp3
  020_Spinal_Support_Powerful_Pelvis_b61e70.mp3
  manifest.jsonl
```

Each manifest line:
```json
{"url": "https://...", "filename": "lesson.mp3", "local_path": "downloads/lesson.mp3", "downloaded_at": "2026-05-11T12:00:00Z"}
```

Downloads are crash-safe: files are written to a `.part` temporary and renamed atomically. Already-downloaded files are skipped on re-runs.
