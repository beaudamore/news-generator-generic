"""news-generator helper: a ComfyUI custom node package that adds model-download routes.

ComfyUI's own HTTP API can list models (GET /models, GET /models/{folder}) but has no
public route to fetch one. This package registers no nodes; it only adds routes on the
ComfyUI server so the news-generator app can ask the host to download the files a
workflow needs into the right models/ folder.

Routes (all relative to the ComfyUI base URL):

    GET  /news-generator/helper                 {"name", "version", "folders": [...], "allowed_hosts": [...]}
    GET  /news-generator/models/files/{folder}  {"folder", "files": [...]} the files ComfyUI sees in that folder
    POST /news-generator/models/download        body: {"url", "folder", "filename", "sha256"?, "token"?, "subfolder"?}
                                                -> 202 {"job": {...}} or 200 {"job": {"status": "exists"}}
    GET  /news-generator/models/jobs            {"jobs": [...]}
    GET  /news-generator/models/jobs/{job_id}   {"job": {...}}
    POST /news-generator/models/jobs/{job_id}/cancel

A job is {"id", "status", "url", "folder", "filename", "path", "bytes_done", "bytes_total",
"percent", "speed_bps", "error", "created_at", "started_at", "finished_at"}; status is one of
queued, downloading, verifying, done, exists, failed, cancelled.

Downloads run one at a time in a background thread, write to "<filename>.part" and rename on
success, resume a partial file with a Range request, and verify sha256 when one is given.
Only URLs whose host is in NEWS_GENERATOR_DOWNLOAD_ALLOWED_HOSTS (comma-separated; default
huggingface.co, hf.co, civitai.com) are accepted; redirects (to the HF CDN, for example) are
followed. A Hugging Face token comes from the request body, else HF_TOKEN / HUGGINGFACE_TOKEN
in the ComfyUI process environment; a Civitai token from the body, else CIVITAI_API_TOKEN.

Install: copy or symlink this folder into ComfyUI/custom_nodes/ and restart ComfyUI. It
needs nothing beyond the standard library and what ComfyUI already ships.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

HELPER_VERSION = "0.1.0"
DEFAULT_ALLOWED_HOSTS = ("huggingface.co", "hf.co", "civitai.com")
CHUNK_BYTES = 8 * 1024 * 1024
MAX_JOBS_KEPT = 200

NODE_CLASS_MAPPINGS: dict[str, Any] = {}
NODE_DISPLAY_NAME_MAPPINGS: dict[str, str] = {}
WEB_DIRECTORY = None

_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")
_SUBFOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*(/[A-Za-z0-9][A-Za-z0-9._ -]*)*$")


def _allowed_hosts() -> tuple[str, ...]:
    raw = os.environ.get("NEWS_GENERATOR_DOWNLOAD_ALLOWED_HOSTS", "").strip()
    if not raw:
        return DEFAULT_ALLOWED_HOSTS
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def _host_allowed(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == allowed or host.endswith("." + allowed) for allowed in _allowed_hosts())


def _default_token(url: str) -> Optional[str]:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host.endswith("huggingface.co") or host.endswith("hf.co"):
        return os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN") or None
    if host.endswith("civitai.com"):
        return os.environ.get("CIVITAI_API_TOKEN") or None
    return None


class DownloadJob:
    def __init__(self, url: str, folder: str, filename: str, path: Path, sha256: Optional[str], token: Optional[str]) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.folder = folder
        self.filename = filename
        self.path = path
        self.sha256 = (sha256 or "").lower() or None
        self.token = token
        self.status = "queued"
        self.bytes_done = 0
        self.bytes_total: Optional[int] = None
        self.speed_bps = 0.0
        self.error: Optional[str] = None
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.cancel_requested = False

    def to_dict(self) -> dict[str, Any]:
        percent = None
        if self.bytes_total:
            percent = round(100.0 * self.bytes_done / self.bytes_total, 2)
        elif self.status in ("done", "exists"):
            percent = 100.0
        return {
            "id": self.id,
            "status": self.status,
            "url": self.url,
            "folder": self.folder,
            "filename": self.filename,
            "path": str(self.path),
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "percent": percent,
            "speed_bps": int(self.speed_bps),
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class Downloader:
    """One worker thread draining a FIFO of jobs; jobs are kept (bounded) for status queries."""

    def __init__(self) -> None:
        self.jobs: "OrderedDict[str, DownloadJob]" = OrderedDict()
        self.queue: list[DownloadJob] = []
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def _ensure_thread(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, name="news-generator-downloader", daemon=True)
            self.thread.start()

    def find_active(self, path: Path) -> Optional[DownloadJob]:
        for job in self.jobs.values():
            if job.path == path and job.status in ("queued", "downloading", "verifying"):
                return job
        return None

    def submit(self, job: DownloadJob) -> DownloadJob:
        with self.lock:
            self.jobs[job.id] = job
            while len(self.jobs) > MAX_JOBS_KEPT:
                oldest_id, oldest = next(iter(self.jobs.items()))
                if oldest.status in ("queued", "downloading", "verifying"):
                    break
                self.jobs.pop(oldest_id)
            self.queue.append(job)
            self._ensure_thread()
        self.wake.set()
        return job

    def cancel(self, job_id: str) -> Optional[DownloadJob]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            if job.status == "queued":
                self.queue = [j for j in self.queue if j.id != job_id]
                job.status = "cancelled"
                job.finished_at = time.time()
            elif job.status in ("downloading", "verifying"):
                job.cancel_requested = True
            return job

    def _run(self) -> None:
        while True:
            with self.lock:
                job = self.queue.pop(0) if self.queue else None
            if job is None:
                self.wake.wait(timeout=5)
                self.wake.clear()
                continue
            try:
                self._download(job)
            except _Cancelled:
                job.status = "cancelled"
                job.error = None
            except Exception as exc:  # noqa: BLE001 - every failure must land on the job
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {str(exc)[:300]}"
            finally:
                if job.finished_at is None:
                    job.finished_at = time.time()

    def _download(self, job: DownloadJob) -> None:
        job.status = "downloading"
        job.started_at = time.time()
        job.path.parent.mkdir(parents=True, exist_ok=True)
        part = job.path.with_name(job.path.name + ".part")
        resume_from = part.stat().st_size if part.exists() else 0

        headers = {"User-Agent": f"news-generator-helper/{HELPER_VERSION}"}
        if job.token:
            headers["Authorization"] = f"Bearer {job.token}"
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"
        request = urllib.request.Request(job.url, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=60)  # noqa: S310 - host checked at submit
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and resume_from:  # the .part is already complete
                response = None
            else:
                hint = " (gated file: pass a token or set HF_TOKEN on the ComfyUI host)" if exc.code in (401, 403) else ""
                raise RuntimeError(f"HTTP {exc.code} from {job.url}{hint}") from exc

        if response is not None:
            with response:
                status = getattr(response, "status", 200)
                if status == 206:
                    job.bytes_done = resume_from
                    mode = "ab"
                else:
                    resume_from = 0
                    job.bytes_done = 0
                    mode = "wb"
                length = response.headers.get("Content-Length")
                if length and length.isdigit():
                    job.bytes_total = int(length) + (resume_from if status == 206 else 0)
                window_start, window_bytes = time.time(), 0
                with part.open(mode) as out:
                    while True:
                        if job.cancel_requested:
                            raise _Cancelled()
                        chunk = response.read(CHUNK_BYTES)
                        if not chunk:
                            break
                        out.write(chunk)
                        job.bytes_done += len(chunk)
                        window_bytes += len(chunk)
                        elapsed = time.time() - window_start
                        if elapsed >= 1.0:
                            job.speed_bps = window_bytes / elapsed
                            window_start, window_bytes = time.time(), 0
        job.speed_bps = 0.0
        if job.bytes_total is not None and part.stat().st_size != job.bytes_total:
            raise RuntimeError(f"size mismatch: got {part.stat().st_size} bytes, expected {job.bytes_total}")

        if job.sha256:
            job.status = "verifying"
            digest = hashlib.sha256()
            with part.open("rb") as handle:
                while True:
                    if job.cancel_requested:
                        raise _Cancelled()
                    block = handle.read(CHUNK_BYTES)
                    if not block:
                        break
                    digest.update(block)
            if digest.hexdigest() != job.sha256:
                part.unlink(missing_ok=True)
                raise RuntimeError(f"sha256 mismatch: got {digest.hexdigest()}, expected {job.sha256}")

        os.replace(part, job.path)
        job.bytes_total = job.path.stat().st_size
        job.bytes_done = job.bytes_total
        job.status = "done"
        job.finished_at = time.time()


class _Cancelled(Exception):
    pass


DOWNLOADER = Downloader()


# ----------------------------------------------------------------------------- routes

try:
    from aiohttp import web
    from server import PromptServer  # ComfyUI's server module
    import folder_paths
except Exception:  # pragma: no cover - imported outside ComfyUI (tests, linting)
    PromptServer = None  # type: ignore[assignment]
    folder_paths = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]


def resolve_target(folder: str, filename: str, subfolder: str = "") -> Path:
    """The absolute path a model would be written to, inside the first configured path of that folder."""
    if folder_paths is None:
        raise RuntimeError("folder_paths unavailable outside ComfyUI")
    if not _FILENAME_RE.fullmatch(filename or "") or filename in (".", ".."):
        raise ValueError("filename must be a plain file name (no path separators)")
    if subfolder and not _SUBFOLDER_RE.fullmatch(subfolder):
        raise ValueError("subfolder must be a relative path without '..'")
    paths = folder_paths.get_folder_paths(folder)
    if not paths:
        raise ValueError(f"unknown models folder {folder!r}; known: {sorted(folder_paths.folder_names_and_paths)}")
    root = Path(paths[0]).resolve()
    target = (root / subfolder / filename).resolve() if subfolder else (root / filename).resolve()
    if root not in target.parents:
        raise ValueError("target escapes the models folder")
    return target


def _json_error(status: int, message: str):
    return web.json_response({"error": message}, status=status)


if PromptServer is not None:
    routes = PromptServer.instance.routes

    @routes.get("/news-generator/helper")
    async def helper_info(_request):
        return web.json_response({
            "name": "news-generator-helper",
            "version": HELPER_VERSION,
            "folders": sorted(folder_paths.folder_names_and_paths),
            "allowed_hosts": list(_allowed_hosts()),
            "has_hf_token": bool(os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")),
            "has_civitai_token": bool(os.environ.get("CIVITAI_API_TOKEN")),
        })

    @routes.get("/news-generator/models/files/{folder}")
    async def list_folder(request):
        folder = request.match_info["folder"]
        if folder not in folder_paths.folder_names_and_paths:
            return _json_error(404, f"unknown models folder {folder!r}")
        return web.json_response({"folder": folder, "files": folder_paths.get_filename_list(folder), "paths": folder_paths.get_folder_paths(folder)})

    @routes.post("/news-generator/models/download")
    async def start_download(request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _json_error(400, "body must be JSON")
        if not isinstance(body, dict):
            return _json_error(400, "body must be a JSON object")
        url = str(body.get("url") or "").strip()
        folder = str(body.get("folder") or "").strip()
        filename = str(body.get("filename") or "").strip() or Path(urllib.parse.urlparse(url).path).name
        subfolder = str(body.get("subfolder") or "").strip().strip("/")
        sha256 = str(body.get("sha256") or "").strip().lower() or None
        token = str(body.get("token") or "").strip() or _default_token(url)
        if not url.lower().startswith("https://"):
            return _json_error(400, "url must be https")
        if not _host_allowed(url):
            return _json_error(400, f"host not allowed; allowed: {list(_allowed_hosts())}")
        if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
            return _json_error(400, "sha256 must be 64 hex characters")
        try:
            target = resolve_target(folder, filename, subfolder)
        except ValueError as exc:
            return _json_error(400, str(exc))

        if target.exists() and not body.get("overwrite"):
            job = DownloadJob(url, folder, filename, target, sha256, None)
            job.status = "exists"
            job.bytes_total = job.bytes_done = target.stat().st_size
            job.finished_at = time.time()
            return web.json_response({"job": job.to_dict()}, status=200)
        active = DOWNLOADER.find_active(target)
        if active is not None:
            return web.json_response({"job": active.to_dict()}, status=202)
        job = DOWNLOADER.submit(DownloadJob(url, folder, filename, target, sha256, token))
        return web.json_response({"job": job.to_dict()}, status=202)

    @routes.get("/news-generator/models/jobs")
    async def list_jobs(_request):
        with DOWNLOADER.lock:
            jobs = [job.to_dict() for job in DOWNLOADER.jobs.values()]
        return web.json_response({"jobs": jobs})

    @routes.get("/news-generator/models/jobs/{job_id}")
    async def get_job(request):
        job = DOWNLOADER.jobs.get(request.match_info["job_id"])
        if job is None:
            return _json_error(404, "job not found")
        return web.json_response({"job": job.to_dict()})

    @routes.post("/news-generator/models/jobs/{job_id}/cancel")
    async def cancel_job(request):
        job = DOWNLOADER.cancel(request.match_info["job_id"])
        if job is None:
            return _json_error(404, "job not found")
        return web.json_response({"job": job.to_dict()})

    print(f"[news-generator-helper] v{HELPER_VERSION} routes registered under /news-generator/")
