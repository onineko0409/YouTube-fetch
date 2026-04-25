#!/usr/bin/env python3
"""Channel-scoped YouTube downloader with resume + progress tracking.

Requirements:
- yt-dlp installed and available in PATH.
- Python 3.9+ (uses sqlite3 from stdlib).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB = "progress.db"
DEFAULT_LEASE_MINUTES = 30


@dataclass
class VideoRow:
    video_id: str
    url: str
    title: str | None
    uploader: str | None
    upload_date: str | None
    duration: int | None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            video_id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            title TEXT,
            uploader TEXT,
            upload_date TEXT,
            duration INTEGER,
            state TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            file_path TEXT,
            discovered_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            lease_until TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_state ON videos(state)")
    conn.commit()


def ensure_yt_dlp() -> str:
    path = shutil.which("yt-dlp")
    if not path:
        raise RuntimeError(
            "yt-dlp not found in PATH. Install with: python -m pip install -U yt-dlp"
        )
    return path


def run_yt_dlp_json(channel_url: str) -> dict[str, Any]:
    yt_dlp_path = ensure_yt_dlp()
    cmd = [
        yt_dlp_path,
        "--flat-playlist",
        "--dump-single-json",
        channel_url,
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp metadata failed: {proc.stderr.strip()}")

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("yt-dlp returned invalid JSON metadata") from exc


def load_channel_inventory(channel_url: str) -> list[VideoRow]:
    payload = run_yt_dlp_json(channel_url)
    entries = payload.get("entries") or []

    out: list[VideoRow] = []
    for item in entries:
        video_id = item.get("id")
        if not video_id:
            continue
        out.append(
            VideoRow(
                video_id=video_id,
                url=f"https://www.youtube.com/watch?v={video_id}",
                title=item.get("title"),
                uploader=item.get("uploader") or payload.get("uploader"),
                upload_date=item.get("upload_date"),
                duration=item.get("duration"),
            )
        )
    return out


def upsert_inventory(conn: sqlite3.Connection, videos: list[VideoRow]) -> tuple[int, int]:
    inserted = 0
    updated = 0
    now = utc_now_iso()
    for v in videos:
        cur = conn.execute("SELECT video_id FROM videos WHERE video_id = ?", (v.video_id,))
        exists = cur.fetchone() is not None
        if exists:
            conn.execute(
                """
                UPDATE videos
                SET title = COALESCE(?, title),
                    uploader = COALESCE(?, uploader),
                    upload_date = COALESCE(?, upload_date),
                    duration = COALESCE(?, duration),
                    updated_at = ?
                WHERE video_id = ?
                """,
                (v.title, v.uploader, v.upload_date, v.duration, now, v.video_id),
            )
            updated += 1
        else:
            conn.execute(
                """
                INSERT INTO videos (
                    video_id, url, title, uploader, upload_date, duration,
                    state, attempts, discovered_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (v.video_id, v.url, v.title, v.uploader, v.upload_date, v.duration, now, now),
            )
            inserted += 1
    conn.commit()
    return inserted, updated


def reset_stale_in_progress(conn: sqlite3.Connection) -> int:
    now = utc_now_iso()
    cur = conn.execute(
        """
        UPDATE videos
        SET state = 'pending', lease_until = NULL, updated_at = ?
        WHERE state = 'in_progress' AND lease_until IS NOT NULL AND lease_until < ?
        """,
        (now, now),
    )
    conn.commit()
    return cur.rowcount


def claim_next_video(conn: sqlite3.Connection, max_retries: int, lease_minutes: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    now = utc_now_iso()
    cur = conn.execute(
        """
        SELECT *
        FROM videos
        WHERE state IN ('pending', 'failed_retryable')
          AND attempts < ?
        ORDER BY COALESCE(upload_date, '99999999') ASC, discovered_at ASC
        LIMIT 1
        """,
        (max_retries,),
    )
    row = cur.fetchone()
    if row is None:
        return None

    lease_until = (datetime.now(timezone.utc) + timedelta(minutes=lease_minutes)).isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE videos
        SET state = 'in_progress', attempts = attempts + 1, lease_until = ?, updated_at = ?, last_error = NULL
        WHERE video_id = ?
        """,
        (lease_until, now, row["video_id"]),
    )
    conn.commit()

    cur2 = conn.execute("SELECT * FROM videos WHERE video_id = ?", (row["video_id"],))
    return cur2.fetchone()


def mark_done(conn: sqlite3.Connection, video_id: str, file_path: str | None) -> None:
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE videos
        SET state = 'done', file_path = ?, lease_until = NULL, updated_at = ?, last_error = NULL
        WHERE video_id = ?
        """,
        (file_path, now, video_id),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, video_id: str, error: str, retryable: bool) -> None:
    now = utc_now_iso()
    state = "failed_retryable" if retryable else "failed_terminal"
    conn.execute(
        """
        UPDATE videos
        SET state = ?, last_error = ?, lease_until = NULL, updated_at = ?
        WHERE video_id = ?
        """,
        (state, error[:1000], now, video_id),
    )
    conn.commit()


def download_video(
    url: str,
    output_dir: Path,
    *,
    write_subs: bool,
    write_auto_subs: bool,
    embed_subs: bool,
    sub_langs: str,
    write_comments: bool,
    write_thumbnail: bool,
    write_info_json: bool,
) -> str | None:
    yt_dlp_path = ensure_yt_dlp()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(
        output_dir
        / "%(uploader)s"
        / "%(upload_date)s_%(title).120B_[%(id)s]"
        / "%(title).120B_[%(id)s].%(ext)s"
    )
    cmd = [
        yt_dlp_path,
        "-c",
        "--no-progress",
        "--newline",
        "--print",
        "after_move:filepath",
        "-o",
        output_template,
        url,
    ]
    if write_subs:
        cmd.extend(["--write-subs", "--sub-langs", sub_langs])
    if write_auto_subs:
        cmd.extend(["--write-auto-subs", "--sub-langs", sub_langs])
    if embed_subs:
        cmd.append("--embed-subs")
    if write_comments:
        cmd.append("--write-comments")
    if write_thumbnail:
        cmd.append("--write-thumbnail")
    if write_info_json:
        cmd.append("--write-info-json")

    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(err or "yt-dlp download failed")

    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        return None
    return lines[-1]


def is_retryable_error(message: str) -> bool:
    retry_markers = [
        "timed out",
        "429",
        "temporarily",
        "network",
        "connection",
        "unable to download",
    ]
    lowered = message.lower()
    return any(marker in lowered for marker in retry_markers)


def cmd_sync_manifest(args: argparse.Namespace) -> int:
    conn = sqlite3.connect(args.db)
    ensure_db(conn)
    videos = load_channel_inventory(args.channel_url)
    inserted, updated = upsert_inventory(conn, videos)
    print(f"Inventory sync complete. total_seen={len(videos)} inserted={inserted} updated={updated}")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    conn = sqlite3.connect(args.db)
    ensure_db(conn)
    stale = reset_stale_in_progress(conn)
    if stale:
        print(f"Recovered {stale} stale in_progress jobs back to pending.")

    processed = 0
    while True:
        row = claim_next_video(conn, max_retries=args.max_retries, lease_minutes=args.lease_minutes)
        if row is None:
            print("No more eligible items to process.")
            break

        video_id = row["video_id"]
        url = row["url"]
        print(f"Downloading {video_id} ({url})")

        try:
            file_path = download_video(
                url,
                Path(args.output_dir),
                write_subs=args.write_subs,
                write_auto_subs=args.write_auto_subs,
                embed_subs=args.embed_subs,
                sub_langs=args.sub_langs,
                write_comments=args.write_comments,
                write_thumbnail=args.write_thumbnail,
                write_info_json=args.write_info_json,
            )
            mark_done(conn, video_id=video_id, file_path=file_path)
            print(f"Done {video_id} -> {file_path or 'unknown path'}")
        except Exception as exc:
            msg = str(exc)
            retryable = is_retryable_error(msg)
            mark_failed(conn, video_id=video_id, error=msg, retryable=retryable)
            print(f"Failed {video_id} retryable={retryable}: {msg}")

        processed += 1
        if args.max_items and processed >= args.max_items:
            print(f"Stopped after max_items={args.max_items}")
            break

    return 0


def cmd_report(args: argparse.Namespace) -> int:
    conn = sqlite3.connect(args.db)
    ensure_db(conn)
    cur = conn.execute("SELECT state, COUNT(*) FROM videos GROUP BY state ORDER BY state")
    rows = cur.fetchall()
    counts = {state: count for state, count in rows}
    total = sum(counts.values())

    done = counts.get("done", 0)
    pending = counts.get("pending", 0)
    in_progress = counts.get("in_progress", 0)
    failed_retryable = counts.get("failed_retryable", 0)
    failed_terminal = counts.get("failed_terminal", 0)
    skipped = counts.get("skipped", 0)

    success_pct = (done / total * 100.0) if total else 0.0

    print("Progress report")
    print("---------------")
    print(f"total           : {total}")
    print(f"done            : {done}")
    print(f"pending         : {pending}")
    print(f"in_progress     : {in_progress}")
    print(f"failed_retryable: {failed_retryable}")
    print(f"failed_terminal : {failed_terminal}")
    print(f"skipped         : {skipped}")
    print(f"success_rate    : {success_pct:.2f}%")

    if args.show_failures:
        cur2 = conn.execute(
            """
            SELECT video_id, state, attempts, last_error
            FROM videos
            WHERE state IN ('failed_retryable', 'failed_terminal')
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (args.show_failures,),
        )
        failed_rows = cur2.fetchall()
        if failed_rows:
            print("\nRecent failures")
            print("---------------")
            for video_id, state, attempts, last_error in failed_rows:
                print(f"{video_id} [{state}] attempts={attempts} error={last_error}")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="YouTube channel downloader with resume and progress DB")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="Fetch channel inventory and upsert manifest")
    sync.add_argument("--channel-url", required=True, help="Channel URL (e.g. https://youtube.com/@handle/videos)")
    sync.add_argument("--db", default=DEFAULT_DB, help="SQLite database file path")
    sync.set_defaults(func=cmd_sync_manifest)

    dl = sub.add_parser("download", help="Download queued videos with resume support")
    dl.add_argument("--db", default=DEFAULT_DB, help="SQLite database file path")
    dl.add_argument("--output-dir", default="downloads", help="Output folder for downloaded videos")
    dl.add_argument("--max-items", type=int, default=0, help="Optional max items to process this run (0 = unlimited)")
    dl.add_argument("--max-retries", type=int, default=3, help="Max attempts per video before terminal failure")
    dl.add_argument("--lease-minutes", type=int, default=DEFAULT_LEASE_MINUTES, help="Lease duration for in_progress rows")
    dl.add_argument("--write-subs", action="store_true", help="Download human-created subtitles")
    dl.add_argument("--write-auto-subs", action="store_true", help="Download auto-generated subtitles")
    dl.add_argument("--embed-subs", action="store_true", help="Embed subtitles into the downloaded video when possible")
    dl.add_argument("--sub-langs", default="all", help="Subtitle languages for yt-dlp (default: all)")
    dl.add_argument("--write-comments", action="store_true", help="Download comments metadata if extractor supports it")
    dl.add_argument("--write-thumbnail", action="store_true", help="Download thumbnails as sidecar files")
    dl.add_argument(
        "--write-info-json",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write info JSON metadata sidecar (default: enabled)",
    )
    dl.set_defaults(func=cmd_download)

    report = sub.add_parser("report", help="Show progress summary")
    report.add_argument("--db", default=DEFAULT_DB, help="SQLite database file path")
    report.add_argument("--show-failures", type=int, default=0, help="Show latest N failed rows")
    report.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
