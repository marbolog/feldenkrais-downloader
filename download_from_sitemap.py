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


def sync_to_drive(output_dir: str, folder_id: str, service_account_file: str | None = None, auth_port: int = 9090) -> None:
    """Upload every audio file in output_dir that is not already in the Drive folder."""
    import os
    audio_files = sorted(
        f for f in os.listdir(output_dir)
        if os.path.splitext(f)[1].lower() in {".mp3", ".m4a", ".ogg"}
    )
    if not audio_files:
        logger.info("No audio files found in %s/", output_dir)
        return

    logger.info("Syncing %d local file(s) to Google Drive folder %s...", len(audio_files), folder_id)
    drive_service = get_drive_service(service_account_file, auth_port)
    failed = 0
    for filename in audio_files:
        local_path = os.path.join(output_dir, filename)
        try:
            upload_file_to_drive(
                service=drive_service,
                file_path=local_path,
                filename=filename,
                folder_id=folder_id,
            )
        except Exception as exc:
            logger.warning("GDrive upload failed for %s: %s", filename, exc)
            failed += 1

    logger.info(
        "Sync complete: %d file(s) processed, %d failed.",
        len(audio_files) - failed,
        failed,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Download free Feldenkrais lessons via sitemap.")
    parser.add_argument("--output-dir", default="downloads")
    parser.add_argument("--gdrive-folder-id", type=str, default="1DoeAFcPcKXxw25bwXyaooTAWA0BfFLy0")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--download-delay", type=float, default=0.5)
    parser.add_argument(
        "--sync-only",
        action="store_true",
        help="Skip download; upload all local audio files in --output-dir to Drive.",
    )
    parser.add_argument(
        "--service-account-file",
        type=str,
        default=None,
        metavar="SA_JSON",
        help="Path to a GCP service account JSON key for headless Drive auth.",
    )
    parser.add_argument(
        "--auth-port",
        type=int,
        default=9090,
        metavar="PORT",
        help="Local port for the OAuth callback server (default: 9090).",
    )
    args = parser.parse_args()

    setup_logging(args.log_level)

    if args.sync_only:
        if not args.gdrive_folder_id:
            logger.error("--gdrive-folder-id is required for --sync-only.")
            return
        sync_to_drive(args.output_dir, args.gdrive_folder_id, args.service_account_file, args.auth_port)
        return

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
        drive_service = get_drive_service(args.service_account_file, args.auth_port)
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
