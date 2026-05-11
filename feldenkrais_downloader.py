import argparse
import logging
import os
import re
import time
import urllib.parse
from collections import deque

import requests
from bs4 import BeautifulSoup

from utils import (
    BASE_URL,
    download_audio_files,
    fetch_with_retry,
    get_drive_service,
    get_requests_session,
    is_audio_url,
    is_same_site,
    normalize_url,
    setup_logging,
    upload_file_to_drive,
)

DEFAULT_DELAY_SECONDS = 1.0

logger = logging.getLogger(__name__)


def crawl_for_audio_urls(
    start_url: str,
    max_pages: int | None = None,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    path_substring_filter: str | None = None,
) -> set[str]:
    """BFS crawl; return all unique audio file URLs found on the site."""
    session = get_requests_session()
    visited_pages: set[str] = set()
    audio_urls: set[str] = set()
    queue: deque[str] = deque([normalize_url(start_url)])

    while queue:
        page_url = queue.popleft()
        if page_url in visited_pages or not is_same_site(page_url):
            continue

        visited_pages.add(page_url)

        try:
            resp = fetch_with_retry(session, page_url)
        except requests.RequestException as exc:
            logger.warning("Failed to fetch %s: %s", page_url, exc)
            continue

        if not (200 <= resp.status_code < 300):
            logger.warning("Skipping %s, status %s", page_url, resp.status_code)
            continue

        total_hint = max_pages if max_pages is not None else "?"
        logger.info("Parsed page %d of %s: %s", len(visited_pages), total_hint, page_url)

        page_text = resp.text
        soup = BeautifulSoup(page_text, "html.parser")

        def add_audio_link(raw_url: str | None) -> None:
            if not raw_url:
                return
            full = normalize_url(raw_url, base=page_url)
            if is_audio_url(full):
                audio_urls.add(full)

        for a in soup.find_all("a", href=True):
            add_audio_link(a["href"])
        for audio in soup.find_all("audio"):
            add_audio_link(audio.get("src"))
            for source in audio.find_all("source"):
                add_audio_link(source.get("src"))

        for match in re.findall(r"https?://[^\s\"'>]+", page_text, flags=re.IGNORECASE):
            if is_audio_url(match):
                audio_urls.add(normalize_url(match, base=page_url))

        for match in re.findall(
            r"['\"]([^'\">]+\.(?:mp3|m4a|ogg))['\"]", page_text, flags=re.IGNORECASE
        ):
            cleaned = match.replace("\\/", "/")
            if cleaned.startswith("//"):
                cleaned = "https:" + cleaned
            full = normalize_url(cleaned, base=page_url)
            if is_audio_url(full):
                audio_urls.add(full)

        for a in soup.find_all("a", href=True):
            target = normalize_url(a["href"], base=page_url)
            if not is_same_site(target) or target in visited_pages:
                continue
            if path_substring_filter and path_substring_filter not in target:
                continue
            if any(
                skip in target
                for skip in ("/wp-admin", "/cart", "/checkout", "/my-account", "/account", "/login", "/logout")
            ):
                continue
            queue.append(target)

        if max_pages is not None and len(visited_pages) >= max_pages:
            logger.info("Reached max_pages=%d, stopping crawl.", max_pages)
            break

        if delay_seconds > 0:
            time.sleep(delay_seconds)

    return audio_urls


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crawl feldenkraisproject.com and download all publicly available (free) audio lessons.",
    )
    parser.add_argument("--start-url", default=BASE_URL, help="Starting URL for the crawl.")
    parser.add_argument("--output-dir", default="downloads", help="Directory to store downloaded audio files.")
    parser.add_argument("--max-pages", type=int, default=None, help="Maximum number of HTML pages to crawl.")
    parser.add_argument("--crawl-delay", type=float, default=DEFAULT_DELAY_SECONDS, help="Delay between page requests (seconds).")
    parser.add_argument("--download-delay", type=float, default=DEFAULT_DELAY_SECONDS, help="Delay between download submissions (seconds).")
    parser.add_argument("--gdrive-folder-id", type=str, default=None, help="Google Drive folder ID for upload.")
    parser.add_argument("--path-substring-filter", type=str, default=None, help="Only enqueue pages whose URL contains this string.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"], help="Logging verbosity.")
    parser.add_argument("--workers", type=int, default=4, help="Number of parallel download threads.")
    args = parser.parse_args()

    setup_logging(args.log_level)

    logger.info("Starting crawl from %s", args.start_url)
    audio_urls = crawl_for_audio_urls(
        start_url=args.start_url,
        max_pages=args.max_pages,
        delay_seconds=args.crawl_delay,
        path_substring_filter=args.path_substring_filter,
    )

    logger.info("Found %d unique audio URL(s).", len(audio_urls))
    if not audio_urls:
        logger.info("Nothing to download.")
        return

    downloaded_files = download_audio_files(
        audio_urls=audio_urls,
        output_dir=args.output_dir,
        delay_seconds=args.download_delay,
        max_workers=args.workers,
        manifest_name="manifest.jsonl",
    )

    if args.gdrive_folder_id and downloaded_files:
        logger.info("Uploading %d file(s) to Google Drive...", len(downloaded_files))
        drive_service = get_drive_service()
        for local_path in downloaded_files:
            filename = os.path.basename(local_path)
            logger.info("GDrive: %s -> folder %s", filename, args.gdrive_folder_id)
            try:
                upload_file_to_drive(
                    service=drive_service,
                    file_path=local_path,
                    filename=filename,
                    folder_id=args.gdrive_folder_id,
                )
            except Exception as exc:
                logger.warning("GDrive upload failed for %s: %s", filename, exc)

    logger.info("Done.")


if __name__ == "__main__":
    main()
