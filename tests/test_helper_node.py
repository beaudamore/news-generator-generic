"""Tests for the ComfyUI helper node's download engine (comfyui-helper-node/news_generator_helper).

The module is imported outside ComfyUI, so its routes are not registered; the Downloader,
DownloadJob and resolve_target are exercised directly against a local HTTP server.
Run: python -m pytest tests/ -q
"""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import socketserver
import sys
import threading
import time
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = PROJECT_ROOT / "comfyui-helper-node" / "news_generator_helper" / "__init__.py"


@pytest.fixture(scope="module")
def helper():
    spec = importlib.util.spec_from_file_location("news_generator_helper", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


PAYLOAD = bytes(range(256)) * 4096  # 1 MiB, resumable


class Handler(http.server.BaseHTTPRequestHandler):
    """Serves PAYLOAD at /model.bin with Range support; /gated needs a bearer token; /flaky-once cuts off the first request."""

    cut_once = {"pending": True}

    def log_message(self, *_: object) -> None:  # quiet
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/gated" and self.headers.get("Authorization") != "Bearer secret":
            self.send_response(401)
            self.end_headers()
            return
        data = PAYLOAD
        start = 0
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0])
            if start >= len(data):
                self.send_response(416)
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            self.send_response(200)
        body = data[start:]
        if self.path == "/flaky-once" and Handler.cut_once["pending"]:
            Handler.cut_once["pending"] = False
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[: len(body) // 3])
            self.wfile.flush()
            self.connection.close()  # truncated transfer
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def wait(job, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while job.status in ("queued", "downloading", "verifying") and time.time() < deadline:
        time.sleep(0.05)
    assert job.status not in ("queued", "downloading", "verifying"), f"job still {job.status}"


def test_download_writes_file_and_verifies_sha256(helper, server, tmp_path: Path) -> None:
    target = tmp_path / "checkpoints" / "model.bin"
    job = helper.DownloadJob(f"{server}/model.bin", "checkpoints", "model.bin", target, hashlib.sha256(PAYLOAD).hexdigest(), None)
    helper.Downloader().submit(job)
    wait(job)
    assert job.status == "done", job.error
    assert target.read_bytes() == PAYLOAD
    assert job.bytes_total == len(PAYLOAD) and job.bytes_done == len(PAYLOAD)
    assert not target.with_name("model.bin.part").exists()


def test_sha256_mismatch_fails_and_removes_partial(helper, server, tmp_path: Path) -> None:
    target = tmp_path / "model.bin"
    job = helper.DownloadJob(f"{server}/model.bin", "checkpoints", "model.bin", target, "0" * 64, None)
    helper.Downloader().submit(job)
    wait(job)
    assert job.status == "failed" and "sha256 mismatch" in (job.error or "")
    assert not target.exists() and not target.with_name("model.bin.part").exists()


def test_resumes_a_truncated_download(helper, server, tmp_path: Path) -> None:
    target = tmp_path / "model.bin"
    Handler.cut_once["pending"] = True
    first = helper.DownloadJob(f"{server}/flaky-once", "checkpoints", "model.bin", target, None, None)
    downloader = helper.Downloader()
    downloader.submit(first)
    wait(first)
    assert first.status == "failed" and "size mismatch" in (first.error or "")
    part = target.with_name("model.bin.part")
    assert part.exists() and 0 < part.stat().st_size < len(PAYLOAD)

    second = helper.DownloadJob(f"{server}/flaky-once", "checkpoints", "model.bin", target, hashlib.sha256(PAYLOAD).hexdigest(), None)
    downloader.submit(second)
    wait(second)
    assert second.status == "done", second.error
    assert target.read_bytes() == PAYLOAD


def test_gated_url_needs_token(helper, server, tmp_path: Path) -> None:
    target = tmp_path / "gated.bin"
    denied = helper.DownloadJob(f"{server}/gated", "checkpoints", "gated.bin", target, None, None)
    helper.Downloader().submit(denied)
    wait(denied)
    assert denied.status == "failed" and "HTTP 401" in denied.error and "token" in denied.error

    allowed = helper.DownloadJob(f"{server}/gated", "checkpoints", "gated.bin", target, None, "secret")
    helper.Downloader().submit(allowed)
    wait(allowed)
    assert allowed.status == "done", allowed.error


def test_resume_is_bound_to_the_url(helper, server, tmp_path: Path) -> None:
    """A .part left by a different URL is discarded instead of being appended to."""
    target = tmp_path / "model.bin"
    part = target.with_name("model.bin.part")
    part.write_bytes(b"garbage from somewhere else")
    part.with_name(part.name + ".url").write_text("https://other.example/old")
    job = helper.DownloadJob(f"{server}/model.bin", "checkpoints", "model.bin", target, hashlib.sha256(PAYLOAD).hexdigest(), None)
    helper.Downloader().submit(job)
    wait(job)
    assert job.status == "done", job.error
    assert target.read_bytes() == PAYLOAD


def test_size_cap_refuses_large_files(helper, server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWS_GENERATOR_DOWNLOAD_MAX_GB", str(0.5 * len(PAYLOAD) / 1024**3))
    target = tmp_path / "model.bin"
    job = helper.DownloadJob(f"{server}/model.bin", "checkpoints", "model.bin", target, None, None)
    helper.Downloader().submit(job)
    wait(job)
    assert job.status == "failed" and "MAX_GB" in job.error
    assert not target.exists()


def test_queue_is_bounded(helper, tmp_path: Path) -> None:
    downloader = helper.Downloader()
    downloader._ensure_thread = lambda: None  # never start the worker; just fill the queue
    for i in range(helper.MAX_QUEUED):
        downloader.submit(helper.DownloadJob("https://huggingface.co/a/b/resolve/main/c.safetensors", "loras", f"{i}.safetensors", tmp_path / f"{i}.safetensors", None, None))
    with pytest.raises(RuntimeError, match="queue full"):
        downloader.submit(helper.DownloadJob("https://huggingface.co/a/b/resolve/main/c.safetensors", "loras", "x.safetensors", tmp_path / "x.safetensors", None, None))


def test_redirects_drop_authorization_across_hosts_and_refuse_http(helper) -> None:
    import urllib.request
    handler = helper._SafeRedirects()
    req = urllib.request.Request("https://huggingface.co/a/b/resolve/main/c.safetensors", headers={"Authorization": "Bearer secret", "User-Agent": "x"})
    same = handler.redirect_request(req, None, 302, "Found", {}, "https://huggingface.co/other")
    assert same.get_header("Authorization") == "Bearer secret"
    other = handler.redirect_request(req, None, 302, "Found", {}, "https://cas-bridge.xethub.hf.co/signed")
    assert other.get_header("Authorization") is None
    import urllib.error
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://huggingface.co/plain")


def test_cancel_queued_job(helper, tmp_path: Path) -> None:
    downloader = helper.Downloader()
    job = helper.DownloadJob("https://huggingface.co/x/y/resolve/main/z.safetensors", "loras", "z.safetensors", tmp_path / "z.safetensors", None, None)
    with downloader.lock:  # keep the worker from picking it up before we cancel
        downloader.jobs[job.id] = job
        downloader.queue.append(job)
    cancelled = downloader.cancel(job.id)
    assert cancelled is job and job.status == "cancelled"
    assert downloader.cancel("nope") is None


def test_allowed_hosts(helper, monkeypatch: pytest.MonkeyPatch) -> None:
    assert helper._host_allowed("https://huggingface.co/a/b/resolve/main/c.safetensors")
    assert helper._host_allowed("https://cdn-lfs.hf.co/whatever")
    assert helper._host_allowed("https://civitai.com/api/download/models/1")
    assert not helper._host_allowed("https://example.com/model.safetensors")
    monkeypatch.setenv("NEWS_GENERATOR_DOWNLOAD_ALLOWED_HOSTS", "models.internal.lan")
    assert helper._host_allowed("https://models.internal.lan/x.gguf")
    assert not helper._host_allowed("https://huggingface.co/a/b")


def test_resolve_target_stays_inside_models_folder(helper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "models" / "loras"
    root.mkdir(parents=True)
    nodes = tmp_path / "custom_nodes"
    nodes.mkdir()
    table = {"loras": [str(root)], "custom_nodes": [str(nodes)], "diffusion_models": [str(tmp_path / "models" / "unet")]}
    fake = types.SimpleNamespace(folder_names_and_paths={k: (v, set()) for k, v in table.items()},
                                 get_folder_paths=lambda f: table.get("diffusion_models" if f == "unet" else f, []))
    monkeypatch.setattr(helper, "folder_paths", fake)
    # folders ComfyUI executes from are refused even though folder_paths knows them
    with pytest.raises(ValueError, match="not a model folder"):
        helper.resolve_target("custom_nodes", "evil.safetensors")
    # only model file types
    for bad_name in ("evil.py", "x.sh", "x.json", "x"):
        with pytest.raises(ValueError, match="must end with"):
            helper.resolve_target("loras", bad_name)
    # the legacy folder name resolves through get_folder_paths' mapping
    assert helper.resolve_target("unet", "m.gguf").parent == (tmp_path / "models" / "unet").resolve()
    assert helper.resolve_target("loras", "x.safetensors") == (root / "x.safetensors").resolve()
    assert helper.resolve_target("loras", "x.safetensors", "ltx/sub") == (root / "ltx" / "sub" / "x.safetensors").resolve()
    for bad in ("../x.safetensors", "a/b.safetensors", "", ".."):
        with pytest.raises(ValueError):
            helper.resolve_target("loras", bad)
    with pytest.raises(ValueError):
        helper.resolve_target("loras", "x.safetensors", "../escape")
    with pytest.raises(ValueError):
        helper.resolve_target("nope", "x.safetensors")


def test_module_imports_without_comfyui(helper) -> None:
    assert helper.NODE_CLASS_MAPPINGS == {}
    assert "news_generator_helper" in sys.modules or helper.HELPER_VERSION
