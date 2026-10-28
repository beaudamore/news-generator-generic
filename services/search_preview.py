"""Search preview and profile defaults: see what a profile would select before spending a run on it."""

from __future__ import annotations

import importlib
import os
from types import SimpleNamespace
from typing import Any, Optional

from services.errors import ApiError, invalid
from services.models import Tenant


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def defaults() -> dict[str, Any]:
    """The values a profile field takes when left blank, straight from .env."""
    return {
        "max_articles": _env_int("SEARXNG_DEFAULT_MAX_ARTICLES", 5),
        "max_age_days": _env_int("SEARXNG_DEFAULT_MAX_AGE_DAYS", 1),
        "categories": [c.strip() for c in os.environ.get("SEARXNG_DEFAULT_CATEGORIES", "news,general").split(",") if c.strip()],
        "language": os.environ.get("SEARXNG_DEFAULT_LANGUAGE", "en").strip() or "en",
        "safesearch": os.environ.get("SEARXNG_DEFAULT_SAFESEARCH", "1").strip() or "1",
        "intended_video_max_seconds": _env_float("NEWS_INTENDED_VIDEO_MAX_SECONDS", 30.0),
        "wpm": _env_float("NEWS_DEFAULT_WPM", 145.0),
        "rank_candidates": _env_int("NEWS_SOURCE_RANK_CANDIDATES", 50),
        "rank_enabled": os.environ.get("NEWS_SOURCE_RANK_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on"),
    }


_categories_cache: dict[str, Any] = {"at": 0.0, "value": None}


def search_categories() -> list[str]:
    import time

    now = time.time()
    if _categories_cache["value"] is None or now - _categories_cache["at"] > 300:
        base = os.environ.get("SEARXNG_BASE_URL", "").strip().rstrip("/")
        if not base:
            raise ApiError("service_unavailable", "SEARXNG_BASE_URL is not set.")
        try:
            response = importlib.import_module("requests").get(f"{base}/config", timeout=10)
            response.raise_for_status()
            _categories_cache.update(at=now, value=list(response.json().get("categories") or []))
        except Exception as exc:  # noqa: BLE001
            raise ApiError("service_unavailable", f"SearXNG did not answer: {exc}") from exc
    return list(_categories_cache["value"])


def options() -> dict[str, Any]:
    return {"categories": search_categories(), "defaults": defaults()}


def preview(
    tenant: Tenant,
    search_text: str,
    categories: Optional[str] = None,
    language: Optional[str] = None,
    max_articles: Optional[int] = None,
    max_age_days: Optional[int] = None,
    rank_query: Optional[str] = None,
    rank_candidates: Optional[int] = None,
    extract_articles: bool = False,
) -> dict[str, Any]:
    """Run search plus ranking exactly as a run would, and return what it would select. No LLM, no run directory."""
    search_text = (search_text or "").strip()
    if not search_text:
        raise invalid("search_text is required.")
    d = defaults()
    search = importlib.import_module("scripts.search_searxng")
    args = SimpleNamespace(
        categories=(categories or "").strip() or None,
        language=(language or "").strip() or None,
        safesearch=None,
        max_articles=int(max_articles) if max_articles else d["max_articles"],
        max_age_days=int(max_age_days) if max_age_days is not None else d["max_age_days"],
        time_range=os.environ.get("SEARXNG_DEFAULT_TIME_RANGE") or None,
        timeout=_env_int("SEARXNG_DEFAULT_TIMEOUT_SECONDS", 20),
        rank_query=(rank_query or "").strip() or search_text,
        rank_candidates=int(rank_candidates) if rank_candidates else None,
    )
    try:
        result = search.search_searxng(search_text, args)
    except Exception as exc:  # noqa: BLE001
        raise ApiError("service_unavailable", f"SearXNG search failed: {exc}") from exc
    data = result["data"]
    raw = data.get("results") or []
    selected, rank = search.select_and_rank_sources(raw, args)
    from services.prompting import outlet_name

    extraction: dict[str, Any] = {}
    if extract_articles and selected:
        from services import extract

        extraction = extract.extract_sources([dict(s) for s in selected])
        for s, copy in zip(selected, extraction.get("sources", [])):
            s["extraction"] = copy.get("extraction")

    def item(s: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": s.get("title"),
            "url": s.get("url"),
            "source": s.get("source"),
            "outlet": outlet_name(s.get("source") or ""),
            "published_at": s.get("published_at"),
            "score": s.get("source_rank_score"),
            "snippet": (s.get("content") or "")[:300],
            "engines": s.get("engines") or [],
            "extraction": s.get("extraction"),
        }

    dated = sum(1 for r in raw if r.get("publishedDate"))
    return {
        "search_text": search_text,
        "request_url": result["request_url"],
        "raw_count": len(raw),
        "dated_count": dated,
        "candidate_count": rank.get("candidate_count", len(selected)),
        "selected": [item(s) for s in selected],
        "rank": {k: v for k, v in rank.items() if k in ("enabled", "query", "model", "degraded", "error")},
        "unresponsive_engines": [list(u)[:2] for u in (data.get("unresponsive_engines") or [])],
        "extraction": {k: v for k, v in extraction.items() if k != "sources"} if extraction else None,
        "effective": {
            "categories": args.categories or ",".join(d["categories"]),
            "language": args.language or d["language"],
            "max_articles": args.max_articles,
            "max_age_days": args.max_age_days,
            "rank_query": args.rank_query,
        },
    }
