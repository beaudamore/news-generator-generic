"""Article text extraction through a crawl4ai server.

Best effort, per source: a run never fails because an article could not be
fetched. Each selected source gets an `extraction` record and, when extraction
succeeds, its `content` becomes the article body instead of the search snippet.
The original snippet is kept as `snippet`.

Configuration (all optional; extraction is off when CRAWL4AI_API_URL is unset):
  CRAWL4AI_API_URL, CRAWL4AI_API_TOKEN     the crawl4ai REST server and its bearer token
  ARTICLE_EXTRACTION_ENABLED=true          master switch
  ARTICLE_EXTRACT_FILTER=fit               crawl4ai markdown filter: fit, raw, bm25
  ARTICLE_EXTRACT_TIMEOUT_SECONDS=45       per article
  ARTICLE_EXTRACT_MAX_WORDS=1500           body is cut here
  ARTICLE_EXTRACT_MIN_WORDS=120            below this the result is treated as failed
  ARTICLE_EXTRACT_CONCURRENCY=3            parallel fetches per run
  ARTICLE_EXTRACT_SKIP_DOMAINS=...         comma list; snippet is used for these (video, social)
  ARTICLE_EXTRACT_USER_AGENT=...           browser user agent sent by crawl4ai (a desktop Chrome string by default)
  ARTICLE_EXTRACT_PRUNE_THRESHOLD=0.48     crawl4ai PruningContentFilter threshold for the fit markdown
"""

from __future__ import annotations

import importlib
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
from urllib.parse import urlsplit

LOGGER = logging.getLogger("news_pipeline")

DEFAULT_SKIP_DOMAINS = "youtube.com,youtu.be,facebook.com,instagram.com,tiktok.com,x.com,twitter.com,reddit.com,threads.net,pinterest.com"
BLOCK_MARKERS = (
    "access denied", "unusual traffic", "has been blocked", "enable javascript", "are you a robot", "verify you are human",
    "captcha", "403 forbidden", "subscribe to continue", "please enable cookies", "checking your browser", "attention required",
)
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def enabled() -> bool:
    return bool(_env("CRAWL4AI_API_URL", "")) and _env("ARTICLE_EXTRACTION_ENABLED", "true").lower() in ("1", "true", "yes", "on")


def settings() -> dict[str, Any]:
    return {
        "url": _env("CRAWL4AI_API_URL", "").rstrip("/"),
        "token": _env("CRAWL4AI_API_TOKEN", ""),
        "filter": _env("ARTICLE_EXTRACT_FILTER", "fit"),
        "timeout": int(_env("ARTICLE_EXTRACT_TIMEOUT_SECONDS", "45")),
        "max_words": int(_env("ARTICLE_EXTRACT_MAX_WORDS", "1500")),
        "min_words": int(_env("ARTICLE_EXTRACT_MIN_WORDS", "120")),
        "concurrency": max(1, int(_env("ARTICLE_EXTRACT_CONCURRENCY", "3"))),
        "skip_domains": [d.strip().lower() for d in _env("ARTICLE_EXTRACT_SKIP_DOMAINS", DEFAULT_SKIP_DOMAINS).split(",") if d.strip()],
        "user_agent": _env("ARTICLE_EXTRACT_USER_AGENT", DEFAULT_USER_AGENT),
        "prune_threshold": float(_env("ARTICLE_EXTRACT_PRUNE_THRESHOLD", "0.48")),
    }


def _domain_skipped(url: str, skip: list[str]) -> bool:
    host = urlsplit(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in skip)


def clean_markdown(markdown: str) -> str:
    """Turn crawl4ai markdown into plain prose: drop images, unwrap links, strip tags and nav-like lines."""
    text = _IMAGE.sub("", markdown)
    text = _LINK.sub(r"\1", text)
    text = _HTML.sub("", text)
    lines: list[str] = []
    for raw in text.splitlines():
        line = _WS.sub(" ", raw).strip().strip("#*>-| ").strip()
        if not line:
            continue
        if len(line.split()) < 4 and not line.endswith((".", "!", "?")):
            continue  # menu items, buttons, breadcrumbs
        lines.append(line)
    return "\n".join(lines)


def looks_blocked(text: str) -> bool:
    head = text[:1500].lower()
    return any(marker in head for marker in BLOCK_MARKERS)


DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


def _crawl(url: str, cfg: dict[str, Any]) -> tuple[Optional[dict[str, str]], Optional[str]]:
    """POST /crawl with a real browser user agent. Returns ({"fit": ..., "raw": ...}, None) or (None, error)."""
    requests = importlib.import_module("requests")
    headers = {"Authorization": f"Bearer {cfg['token']}"} if cfg["token"] else {}
    body = {
        "urls": [url],
        "browser_config": {
            "type": "BrowserConfig",
            "params": {"headless": True, "user_agent": cfg["user_agent"], "viewport": {"type": "dict", "value": {"width": 1366, "height": 900}}},
        },
        "crawler_config": {
            "type": "CrawlerRunConfig",
            "params": {
                "cache_mode": "bypass",
                "page_timeout": int(cfg["timeout"] * 1000),
                "markdown_generator": {
                    "type": "DefaultMarkdownGenerator",
                    "params": {"content_filter": {"type": "PruningContentFilter", "params": {"threshold": cfg["prune_threshold"], "threshold_type": "fixed"}}},
                },
            },
        },
    }
    response = requests.post(f"{cfg['url']}/crawl", headers=headers, json=body, timeout=cfg["timeout"] + 15)
    if response.status_code != 200:
        detail = ""
        try:
            detail = str(response.json().get("error") or response.json().get("detail") or "")[:160]
        except ValueError:
            detail = response.text[:160]
        return None, f"crawl4ai {response.status_code} {detail}".strip()
    results = response.json().get("results") or []
    if not results:
        return None, "crawl4ai returned no results"
    result = results[0]
    if not result.get("success", False):
        return None, str(result.get("error_message") or "crawl4ai reported failure")[:160]
    markdown = result.get("markdown") or {}
    if isinstance(markdown, str):
        markdown = {"raw_markdown": markdown, "fit_markdown": ""}
    return {"fit": markdown.get("fit_markdown") or "", "raw": markdown.get("raw_markdown") or "", "status_code": result.get("status_code")}, None


def extract_article(url: str, cfg: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Extract one article. Returns {status, words, text, elapsed_ms, filter, error}.

    status: extracted | skipped | blocked | short | error
    """
    cfg = cfg or settings()
    started = time.perf_counter()
    result: dict[str, Any] = {"status": "error", "words": 0, "text": "", "filter": cfg["filter"], "error": None}
    if _domain_skipped(url, cfg["skip_domains"]):
        result.update(status="skipped", error="domain on ARTICLE_EXTRACT_SKIP_DOMAINS")
    else:
        try:
            crawled, error = _crawl(url, cfg)
            if crawled is None:
                result.update(status="error", error=error)
            else:
                order = ["fit", "raw"] if cfg["filter"] != "raw" else ["raw"]
                for flt in order:
                    text = clean_markdown(crawled.get(flt) or "")
                    words = text.split()
                    if looks_blocked(text):
                        result.update(status="blocked", error=f"block or consent page (http {crawled.get('status_code')})", filter=flt)
                        continue
                    if len(words) < cfg["min_words"]:
                        result.update(status="short", words=len(words), error=f"only {len(words)} words", filter=flt)
                        continue
                    if len(words) > cfg["max_words"]:
                        text = " ".join(words[: cfg["max_words"]])
                        words = words[: cfg["max_words"]]
                    result.update(status="extracted", words=len(words), text=text, error=None, filter=flt)
                    break
        except Exception as exc:  # noqa: BLE001 - never let one article fail the run
            result.update(status="error", error=f"{type(exc).__name__}: {str(exc)[:160]}")
    result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return result


def extract_sources(sources: list[dict[str, Any]], run_dir=None, log=None) -> dict[str, Any]:
    """Extract every source in place. Sets source["snippet"], source["content"] (on success), source["extraction"]."""
    cfg = settings()
    summary = {"enabled": enabled(), "extracted": 0, "fallback": 0, "words": 0}
    if not enabled() or not sources:
        for source in sources:
            source.setdefault("snippet", source.get("content") or "")
            source["extraction"] = {"status": "disabled", "words": 0}
        return summary

    def work(indexed: tuple[int, dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        index, source = indexed
        return index, extract_article(str(source.get("url") or ""), cfg)

    with ThreadPoolExecutor(max_workers=cfg["concurrency"]) as pool:
        results = dict(pool.map(work, enumerate(sources)))

    for index, source in enumerate(sources):
        outcome = results[index]
        snippet = source.get("content") or ""
        source["snippet"] = snippet
        record = {k: v for k, v in outcome.items() if k != "text"}
        source["extraction"] = record
        if outcome["status"] == "extracted":
            source["content"] = outcome["text"]
            summary["extracted"] += 1
            summary["words"] += outcome["words"]
            if run_dir is not None:
                folder = run_dir / "articles"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"{index + 1:02d}.md").write_text(f"# {source.get('title') or ''}\n\n{source.get('url') or ''}\n\n{outcome['text']}\n", encoding="utf-8")
        else:
            summary["fallback"] += 1
        if log is not None:
            log("extract", f"source {index + 1}: {outcome['status']} words={outcome['words']} filter={outcome.get('filter')} {outcome['elapsed_ms']} ms {source.get('source')} {('| ' + str(outcome['error'])) if outcome.get('error') else ''}")
    summary["sources"] = sources
    return summary
