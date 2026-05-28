import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BASE_URL = "https://feldenkraisproject.com/"
SCOPES = ["https://www.googleapis.com/auth/drive"]

logger = logging.getLogger(__name__)


def normalize_url(url: str, base: str = BASE_URL) -> str:
    joined = urllib.parse.urljoin(base, url)
    parsed = urllib.parse.urlparse(joined)
    cleaned = parsed._replace(fragment="")
    return urllib.parse.urlunparse(cleaned)


def is_same_site(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    base_parsed = urllib.parse.urlparse(BASE_URL)
    return parsed.scheme in ("http", "https") and parsed.netloc == base_parsed.netloc


def is_audio_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    _, ext = os.path.splitext(parsed.path.lower())
    return ext in {".mp3", ".m4a", ".ogg"}


def ensure_directory(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def get_requests_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        )
    })
    return session


def filename_from_url(url: str, index: int | None = None, slug: str | None = None) -> str:
    """Return a collision-safe filename.

    With index: {index:04d}_{slug or basename}_{hash6}.{ext}
    Without:    {basename}_{hash6}.{ext}  (legacy, used for backward-compat checks)
    """
    path = urllib.parse.urlparse(url).path
    name = os.path.basename(path) or "audio"
    root, ext = os.path.splitext(name)
    ext = ext.lower()
    hash6 = hashlib.sha256(url.encode()).hexdigest()[:6]
    if index is not None:
        base = slug if slug else root
        return f"{index:04d}_{base}_{hash6}{ext}"
    return f"{root}_{hash6}{ext}"


def fetch_with_retry(
    session: requests.Session,
    url: str,
    *,
    retries: int = 3,
    timeout: int = 20,
) -> requests.Response:
    """GET url, retrying on network errors or HTTP 5xx. Does not retry on 4xx."""
    last_exc: Exception | None = None
    resp: requests.Response | None = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code < 500:
                return resp
            if attempt < retries:
                logger.warning("HTTP %s for %s, retry %d/%d", resp.status_code, url, attempt + 1, retries)
                time.sleep(2 ** attempt)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                logger.warning("Request error for %s: %s, retry %d/%d", url, exc, attempt + 1, retries)
                time.sleep(2 ** attempt)
    if last_exc:
        raise last_exc
    return resp  # type: ignore[return-value]  # last 5xx response after exhausting retries


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _download_one(
    url: str,
    output_dir: str,
    manifest_path: str,
    manifest_lock: threading.Lock,
    index: int | None = None,
    slug: str | None = None,
) -> str | None:
    """Download a single audio file. Returns local path on success, None on skip/failure."""
    filename = filename_from_url(url, index=index, slug=slug)
    local_path = os.path.join(output_dir, filename)

    if os.path.exists(local_path):
        logger.info("SKIP already downloaded: %s", filename)
        return None

    # Auto-rename a file that was saved under the legacy (un-indexed) name
    if index is not None:
        old_filename = filename_from_url(url)
        old_path = os.path.join(output_dir, old_filename)
        if os.path.exists(old_path):
            os.rename(old_path, local_path)
            logger.info("RENAMED %s → %s", old_filename, filename)
            return local_path

    logger.info("DOWNLOAD %s", url)
    session = get_requests_session()
    tmp_path = f"{local_path}.part"
    success = False

    for attempt in range(4):  # up to 3 retries
        try:
            with session.get(url, stream=True, timeout=60) as resp:
                if resp.status_code >= 500 and attempt < 3:
                    logger.warning("HTTP %s for %s, retry %d/3", resp.status_code, url, attempt + 1)
                    time.sleep(2 ** attempt)
                    continue
                if not (200 <= resp.status_code < 300):
                    logger.warning("Cannot download %s: HTTP %s", url, resp.status_code)
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                    return None
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                os.replace(tmp_path, local_path)
                success = True
                break
        except requests.RequestException as exc:
            if attempt < 3:
                logger.warning("Error for %s: %s, retry %d/3", url, exc, attempt + 1)
                time.sleep(2 ** attempt)
            else:
                logger.warning("Failed to download %s: %s", url, exc)

    if not success:
        return None

    with manifest_lock:
        try:
            with open(manifest_path, "a", encoding="utf-8") as mf:
                entry = {
                    "url": url,
                    "filename": filename,
                    "local_path": local_path,
                    "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                mf.write(json.dumps(entry) + "\n")
        except OSError as exc:
            logger.warning("Could not write manifest entry: %s", exc)

    return local_path


def download_audio_files(
    audio_urls: set[str],
    output_dir: str,
    *,
    delay_seconds: float = 1.0,
    max_workers: int = 4,
    manifest_name: str = "manifest.jsonl",
    url_to_meta: "dict[str, tuple[int, str]] | None" = None,
) -> list[str]:
    """Download audio files concurrently. Returns list of newly-downloaded local paths.

    url_to_meta maps audio_url → (order_index, lesson_slug) for ordered filenames.
    """
    ensure_directory(output_dir)
    manifest_path = os.path.join(output_dir, manifest_name)
    manifest_lock = threading.Lock()
    downloaded: list[str] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for url in sorted(audio_urls):
            meta = url_to_meta.get(url) if url_to_meta else None
            index, slug = meta if meta else (None, None)
            future = executor.submit(
                _download_one, url, output_dir, manifest_path, manifest_lock, index, slug
            )
            futures[future] = url
            if delay_seconds > 0:
                time.sleep(delay_seconds)

        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                downloaded.append(result)

    logger.info("Downloaded %d new file(s).", len(downloaded))
    return downloaded


def rename_downloaded_files(
    output_dir: str,
    url_to_meta: "dict[str, tuple[int, str]]",
    manifest_name: str = "manifest.jsonl",
) -> int:
    """Rename files in output_dir using url_to_meta index/slug mapping.

    Reads the manifest to discover current filenames, renames files on disk,
    and rewrites the manifest with updated paths. Returns count of files renamed.
    """
    manifest_path = os.path.join(output_dir, manifest_name)
    if not os.path.exists(manifest_path):
        logger.warning("No manifest found at %s — nothing to rename.", manifest_path)
        return 0

    entries = []
    with open(manifest_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    renamed = 0
    for entry in entries:
        url = entry.get("url", "")
        meta = url_to_meta.get(url)
        if not meta:
            continue
        index, slug = meta
        new_filename = filename_from_url(url, index=index, slug=slug)
        current_filename = entry.get("filename", "")
        if current_filename == new_filename:
            continue
        old_path = os.path.join(output_dir, current_filename)
        new_path = os.path.join(output_dir, new_filename)
        if not os.path.exists(old_path):
            logger.debug("SKIP rename (source missing): %s", current_filename)
            continue
        if os.path.exists(new_path):
            logger.info("SKIP rename (target exists): %s", new_filename)
            entry["filename"] = new_filename
            entry["local_path"] = new_path
            continue
        os.rename(old_path, new_path)
        logger.info("RENAMED %s → %s", current_filename, new_filename)
        entry["filename"] = new_filename
        entry["local_path"] = new_path
        renamed += 1

    with open(manifest_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

    return renamed


def get_drive_service(service_account_file: str | None = None, auth_port: int = 9090):
    """Return an authenticated Google Drive v3 service.

    If service_account_file is given, authenticates as a service account (headless).
    Otherwise falls back to OAuth2 using token.json / credentials.json.
    """
    if service_account_file:
        creds = service_account.Credentials.from_service_account_file(
            service_account_file, scopes=SCOPES
        )
        return build("drive", "v3", credentials=creds)

    creds: Credentials | None = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            logger.info(
                "Opening auth server on port %d. "
                "If on a remote machine, forward the port through your relay:\n"
                "  ssh -L %d:localhost:%d ubuntu@204.216.222.110 "
                "-t ssh -L %d:localhost:%d -p 2222 marcello@localhost\n"
                "Then visit the URL printed below in your local browser.",
                auth_port, auth_port, auth_port, auth_port, auth_port,
            )
            creds = flow.run_local_server(port=auth_port, open_browser=False)
        with open("token.json", "w", encoding="utf-8") as token:
            token.write(creds.to_json())
    return build("drive", "v3", credentials=creds)


def list_drive_files(service, folder_id: str) -> list[dict]:
    """Return all non-trashed files in folder_id visible to the current credentials."""
    files_resource = service.files() if callable(getattr(service, "files", None)) else service.files
    query = f"'{folder_id}' in parents and trashed = false"
    results, page_token = [], None
    while True:
        resp = files_resource.list(
            q=query,
            spaces="drive",
            fields="nextPageToken, files(id, name, createdTime, size, md5Checksum)",
            pageSize=1000,
            pageToken=page_token,
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def dedupe_drive_folder(service, folder_id: str, *, dry_run: bool = True) -> None:
    """Find content-identical files in folder_id and delete all but the best-named copy."""
    files_resource = service.files() if callable(getattr(service, "files", None)) else service.files
    all_files = list_drive_files(service, folder_id)
    logger.info("Found %d file(s) in Drive folder.", len(all_files))

    # Group by md5 (content identity); files without md5 (e.g. Google Docs) are skipped.
    by_md5: dict[str, list[dict]] = {}
    for f in all_files:
        md5 = f.get("md5Checksum")
        if md5:
            by_md5.setdefault(md5, []).append(f)

    duplicates = {md5: copies for md5, copies in by_md5.items() if len(copies) > 1}
    if not duplicates:
        logger.info("No content-identical duplicates found.")
        return

    total_extras = sum(len(v) - 1 for v in duplicates.values())
    logger.info("Found %d content group(s) with duplicates (%d file(s) to remove):", len(duplicates), total_extras)
    deleted = 0
    for md5, copies in sorted(duplicates.items(), key=lambda x: x[1][0]["name"]):
        # Prefer the copy whose name matches our hash-suffix convention; else keep oldest.
        copies.sort(key=lambda f: (
            0 if re.search(r"_[0-9a-f]{6}\.(mp3|m4a|ogg)$", f["name"], re.IGNORECASE) else 1,
            f.get("createdTime", ""),
        ))
        keep, *extras = copies
        logger.info("  md5=%s — keeping '%s', removing %d extra(s):", md5[:8], keep["name"], len(extras))
        for extra in extras:
            logger.info("    '%s' (id=%s created=%s)", extra["name"], extra["id"], extra.get("createdTime", "?"))
            if not dry_run:
                files_resource.delete(fileId=extra["id"]).execute()
                deleted += 1

    if dry_run:
        logger.info("Dry run complete — %d file(s) would be deleted. Re-run with --no-dry-run to apply.", total_extras)
    else:
        logger.info("Deleted %d duplicate file(s).", deleted)


def rename_drive_files(
    service,
    folder_id: str,
    url_to_meta: "dict[str, tuple[int, str]]",
) -> int:
    """Rename files in a Drive folder to include order index and lesson slug.

    Computes old→new filename pairs from url_to_meta, lists Drive folder contents,
    and renames matching files via a metadata-only files.update() call.
    Returns count of files renamed.
    """
    files_resource = service.files() if callable(getattr(service, "files", None)) else service.files

    rename_map: dict[str, str] = {}
    for url, (index, slug) in url_to_meta.items():
        old_name = filename_from_url(url)
        new_name = filename_from_url(url, index=index, slug=slug)
        if old_name != new_name:
            rename_map[old_name] = new_name

    if not rename_map:
        logger.info("No Drive files to rename.")
        return 0

    drive_files = list_drive_files(service, folder_id)
    logger.info("Scanning %d Drive file(s) for renames...", len(drive_files))

    renamed = 0
    for f in drive_files:
        new_name = rename_map.get(f["name"])
        if not new_name:
            continue
        try:
            files_resource.update(fileId=f["id"], body={"name": new_name}).execute()
            logger.info("Drive RENAMED %s → %s", f["name"], new_name)
            renamed += 1
        except Exception as exc:
            logger.warning("Drive rename failed for %s: %s", f["name"], exc)

    return renamed


def upload_file_to_drive(service, file_path: str, filename: str, folder_id: str) -> None:
    """Upload file_path to the given Drive folder. Skips if a file with the same name already exists."""
    files_resource = service.files() if callable(getattr(service, "files", None)) else service.files
    safe_name = filename.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"'{folder_id}' in parents and "
        f"name = '{safe_name}' and "
        "trashed = false"
    )
    existing = (
        files_resource.list(q=query, spaces="drive", fields="files(id, name)", pageSize=1)
        .execute()
        .get("files", [])
    )
    if existing:
        logger.info("GDrive: %s already exists, skipping", filename)
        return

    file_metadata: dict[str, object] = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(file_path, resumable=True)
    request = files_resource.create(body=file_metadata, media_body=media, fields="id")
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            logger.debug("GDrive upload %s: %.1f%%", filename, status.progress() * 100)
    logger.info("GDrive: uploaded %s", filename)
