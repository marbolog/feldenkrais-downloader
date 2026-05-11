"""Download all free Feldenkrais lessons by scanning the lesson sitemap directly."""
import argparse
import logging
import re

import requests

from utils import (
    BASE_URL,
    download_audio_files,
    get_drive_service,
    get_requests_session,
    is_audio_url,
    normalize_url,
    setup_logging,
    upload_file_to_drive,
)

LESSON_SITEMAP = "https://feldenkraisproject.com/lesson-sitemap.xml"

logger = logging.getLogger(__name__)


def fetch_lesson_urls(session: requests.Session) -> list[str]:
    resp = session.get(LESSON_SITEMAP, timeout=20)
    resp.raise_for_status()
    return re.findall(r"<loc>(https://feldenkraisproject\.com/lesson/[^<]+)</loc>", resp.text)


def extract_audio_url(page_text: str, page_url: str) -> str | None:
    # Podlove player embeds audio URL as escaped JSON in podlovePlayerCache.add()
    for match in re.findall(r"""["']([^"'<>]+\.(?:mp3|m4a|ogg))["']""", page_text, re.IGNORECASE):
        cleaned = match.replace("\\/", "/")
        if cleaned.startswith("//"):
            cleaned = "https:" + cleaned
        full = normalize_url(cleaned, base=page_url)
        if is_audio_url(full) and "amazonaws" in full:
            return full
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Download free Feldenkrais lessons via sitemap.")
    parser.add_argument("--output-dir", default="downloads")
    parser.add_argument("--gdrive-folder-id", type=str, default="1DoeAFcPcKXxw25bwXyaooTAWA0BfFLy0")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--download-delay", type=float, default=0.5)
    args = parser.parse_args()

    setup_logging(args.log_level)
    session = get_requests_session()

    logger.info("Fetching lesson list from sitemap...")
    lesson_urls = fetch_lesson_urls(session)
    logger.info("Found %d lesson URLs in sitemap.", len(lesson_urls))

    audio_urls: set[str] = set()
    for i, url in enumerate(lesson_urls, 1):
        try:
            resp = session.get(url, timeout=15)
        except requests.RequestException as exc:
            logger.warning("Failed to fetch %s: %s", url, exc)
            continue
        audio = extract_audio_url(resp.text, url)
        if audio:
            audio_urls.add(audio)
            logger.info("[%d/%d] Found audio: %s", i, len(lesson_urls), audio.split("amazonaws.com/")[1])
        else:
            logger.debug("[%d/%d] No audio (patron-only): %s", i, len(lesson_urls), url)

    logger.info("Found %d downloadable audio file(s) out of %d lessons.", len(audio_urls), len(lesson_urls))
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
            import os
            filename = os.path.basename(local_path)
            try:
                upload_file_to_drive(
                    service=drive_service,
                    file_path=local_path,
                    filename=filename,
                    folder_id=args.gdrive_folder_id,
                )
            except Exception as exc:
                logger.warning("GDrive upload failed for %s: %s", filename, exc)

    logger.info("Done. Downloaded %d file(s) to %s/", len(downloaded_files), args.output_dir)


if __name__ == "__main__":
    main()
