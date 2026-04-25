# YouTube-fetch

A channel-scoped YouTube download utility focused on:

- **Backfill + incremental sync**
- **Resume support** via SQLite job states
- **Progress tracking** and failure visibility

> Important: Ensure your use is compliant with YouTube Terms and applicable copyright law.

## Requirements

- Python 3.9+
- `yt-dlp` installed in `PATH`

Install yt-dlp (example):

```bash
python -m pip install -U yt-dlp
```

## Workflow

### 1) Sync channel inventory into manifest DB

```bash
python youtube_channel_sync.py sync \
  --channel-url "https://youtube.com/@koob/videos" \
  --db progress.db
```

This creates/updates `progress.db` with one row per `video_id`.

### 2) Download queue with resume

```bash
python youtube_channel_sync.py download \
  --db progress.db \
  --output-dir downloads \
  --max-retries 3 \
  --write-subs \
  --write-auto-subs \
  --write-comments
```

- Rows move through states: `pending -> in_progress -> done`.
- Recoverable failures go to `failed_retryable`.
- Non-recoverable failures go to `failed_terminal`.
- If interrupted, rerun `download`; stale `in_progress` rows are recovered.
- By default each video is saved in its own folder:
  `downloads/<uploader>/<upload_date>_<title>_[<video_id>]/`

Optional bounded run:

```bash
python youtube_channel_sync.py download --db progress.db --max-items 100
```

### 3) Check progress report

```bash
python youtube_channel_sync.py report --db progress.db --show-failures 20
```

## Subtitles, comments, and metadata

The downloader uses **yt-dlp**, not the official YouTube Data API, for media retrieval.

- `--write-subs`: download creator-provided subtitles
- `--write-auto-subs`: download auto-generated subtitles
- `--embed-subs`: embed subtitles into the media container when possible
- `--sub-langs`: language selector passed to yt-dlp (`all` by default)
- `--write-comments`: write comment metadata (if the extractor supports it)
- `--write-info-json` / `--no-write-info-json`: include or skip sidecar metadata JSON (default is enabled)
- `--write-thumbnail`: download thumbnail sidecar image

Example:

```bash
python youtube_channel_sync.py download \
  --db progress.db \
  --output-dir downloads \
  --write-subs \
  --write-auto-subs \
  --embed-subs \
  --write-comments \
  --sub-langs "en.*,zh.*"
```

## Suggested operating pattern for large channels (~3400 videos)

1. Run `sync` once to build inventory.
2. Run `download` in batches (`--max-items`) to validate stability.
3. Move to continuous runs (cron/systemd) after thresholds are tuned.
4. Re-run `sync` periodically (e.g., every 30-60 minutes) for new uploads.

## Notes

- The script is intentionally channel-scoped via your provided channel URL.
- Resume behavior is driven by DB state, not in-memory queue.
- Output naming includes uploader/date/title/video id for traceability.
