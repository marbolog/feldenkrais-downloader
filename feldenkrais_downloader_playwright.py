import argparse
import logging
import os
import time
import urllib.parse
from collections import deque

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import Page, sync_playwright

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


def crawl_for_lesson_pages(
    start_url: str,
    max_pages: int | None = None,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
) -> set[str]:
    """Crawl the site using plain HTTP to find lesson detail pages under /lesson/."""
    session = get_requests_session()
    visited_pages: set[str] = set()
    lesson_pages: set[str] = set()
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

        logger.info("Crawled page (for links): %s", page_url)
        soup = BeautifulSoup(resp.text, "html.parser")

        for a in soup.find_all("a", href=True):
            target = normalize_url(a["href"], base=page_url)
            if not is_same_site(target):
                continue
            if "/lesson/" in urllib.parse.urlparse(target).path:
                lesson_pages.add(target)
            if target not in visited_pages and not any(
                skip in target
                for skip in ("/wp-admin", "/cart", "/checkout", "/my-account", "/account", "/login", "/logout")
            ):
                queue.append(target)

        if max_pages is not None and len(visited_pages) >= max_pages:
            logger.info("Reached max_pages=%d, stopping crawl.", max_pages)
            break

        if delay_seconds > 0:
            time.sleep(delay_seconds)

    return lesson_pages


def record_network_audio(page: Page, collector) -> None:
    """Attach a response handler that collects audio URLs seen on the network."""
    def handle_response(response) -> None:  # type: ignore[no-untyped-def]
        url = response.url
        ct = (response.headers.get("content-type") or "").lower()
        if "audio" in ct or is_audio_url(url):
            logger.debug("Audio-like response: %s (content-type=%s)", url, ct)
            collector(url)

    page.on("response", handle_response)


def discover_audio_for_lessons(
    lesson_urls,
    network_idle_timeout_ms: int = 8000,
    navigation_timeout_ms: int = 60000,
) -> set[str]:
    """Use headless Chromium to open each lesson page and capture audio URLs from network traffic."""
    audio_urls: set[str] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.set_default_navigation_timeout(navigation_timeout_ms)
            record_network_audio(page, audio_urls.add)

            for url in lesson_urls:
                logger.info("Loading lesson in browser: %s", url)
                try:
                    page.goto(url, wait_until="load")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Navigation error for %s: %s", url, exc)
                    continue
                page.wait_for_timeout(network_idle_timeout_ms)
                for el in page.query_selector_all("audio, audio source"):
                    src = el.get_attribute("src")
                    if src:
                        normalized = normalize_url(src, base=url)
                        if is_audio_url(normalized):
                            audio_urls.add(normalized)
        finally:
            browser.close()

    return audio_urls


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Use Playwright to discover and download free Feldenkrais Project "
            "audio lessons by watching browser network traffic."
        ),
    )
    parser.add_argument("--start-url", default=BASE_URL, help="Starting URL to discover lesson pages.")
    parser.add_argument("--output-dir", default="downloads", help="Directory to store downloaded audio files.")
    parser.add_argument("--max-pages", type=int, default=None, help="Maximum number of HTML pages to crawl for lesson links.")
    parser.add_argument("--network-idle-timeout-ms", type=int, default=8000, help="Time in ms to wait after page load for JS audio.")
    parser.add_argument("--navigation-timeout-ms", type=int, default=60000, help="Maximum time in ms to wait for page navigation.")
    parser.add_argument("--single-lesson-url", type=str, default=None, help="Skip crawling; only process this one lesson URL.")
    parser.add_argument("--gdrive-folder-id", type=str, default=None, help="Google Drive folder ID for upload.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"], help="Logging verbosity.")
    parser.add_argument("--workers", type=int, default=4, help="Number of parallel download threads.")
    args = parser.parse_args()

    setup_logging(args.log_level)

    if args.single_lesson_url:
        lesson_urls = {normalize_url(args.single_lesson_url)}
    else:
        logger.info("Crawling from %s to discover lesson pages...", args.start_url)
        lesson_urls = crawl_for_lesson_pages(
            start_url=args.start_url,
            max_pages=args.max_pages,
        )

    logger.info("Found %d lesson URL(s).", len(lesson_urls))
    if not lesson_urls:
        logger.info("No lesson pages discovered; nothing to do.")
        return

    audio_urls = discover_audio_for_lessons(
        lesson_urls=lesson_urls,
        network_idle_timeout_ms=args.network_idle_timeout_ms,
        navigation_timeout_ms=args.navigation_timeout_ms,
    )

    logger.info("Discovered %d unique audio URL(s).", len(audio_urls))
    if not audio_urls:
        logger.info("No audio URLs discovered; nothing to download.")
        return

    downloaded_files = download_audio_files(
        audio_urls=audio_urls,
        output_dir=args.output_dir,
        delay_seconds=DEFAULT_DELAY_SECONDS,
        max_workers=args.workers,
        manifest_name="manifest_playwright.jsonl",
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

