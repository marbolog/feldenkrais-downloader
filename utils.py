import hashlib
import json
import logging
import os
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BASE_URL = "https://feldenkraisproject.com/"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

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


def filename_from_url(url: str) -> str:
    """Return a collision-safe filename: {basename}_{hash6}.{ext} where hash6 = sha256(url)[:6]."""
    path = urllib.parse.urlparse(url).path
    name = os.path.basename(path) or "audio"
    root, ext = os.path.splitext(name)
    hash6 = hashlib.sha256(url.encode()).hexdigest()[:6]
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
) -> str | None:
    """Download a single audio file. Returns local path on success, None on skip/failure."""
    filename = filename_from_url(url)
    local_path = os.path.join(output_dir, filename)

    if os.path.exists(local_path):
        logger.info("SKIP already downloaded: %s", filename)
        return None

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
) -> list[str]:
    """Download audio files concurrently. Returns list of newly-downloaded local paths."""
    ensure_directory(output_dir)
    manifest_path = os.path.join(output_dir, manifest_name)
    manifest_lock = threading.Lock()
    downloaded: list[str] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for url in sorted(audio_urls):
            future = executor.submit(_download_one, url, output_dir, manifest_path, manifest_lock)
            futures[future] = url
            if delay_seconds > 0:
                time.sleep(delay_seconds)

        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                downloaded.append(result)

    logger.info("Downloaded %d new file(s).", len(downloaded))
    return downloaded


def get_drive_service():
    """Return an authenticated Google Drive v3 service using OAuth2 stored in token.json."""
    creds: Credentials | None = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0, open_browser=False)
        with open("token.json", "w", encoding="utf-8") as token:
            token.write(creds.to_json())
    return build("drive", "v3", credentials=creds)


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
