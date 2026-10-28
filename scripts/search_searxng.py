#!/usr/bin/env python3
"""Search SearXNG and save a source bundle for news generation."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_env_file(path: Path) -> None:
    dotenv = importlib.import_module("dotenv")
    dotenv.load_dotenv(path)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "search"


def canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, "", ""))


def parse_datetime(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def time_range_for_max_age(max_age_days: Optional[int]) -> Optional[str]:
    if max_age_days is None or max_age_days <= 0:
        return None
    if max_age_days <= 1:
        return "day"
    if max_age_days <= 31:
        return "month"
    return "year"


def search_searxng(query: str, args: argparse.Namespace) -> dict[str, Any]:
    requests = importlib.import_module("requests")
    base_url = os.environ.get("SEARXNG_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        raise SystemExit("Set SEARXNG_BASE_URL.")
    params = {
        "q": query,
        "format": "json",
        "categories": args.categories or os.environ.get("SEARXNG_DEFAULT_CATEGORIES", ""),
        "language": args.language or os.environ.get("SEARXNG_DEFAULT_LANGUAGE", ""),
        "safesearch": args.safesearch or os.environ.get("SEARXNG_DEFAULT_SAFESEARCH", ""),
    }
    time_range = args.time_range
    if time_range:
        params["time_range"] = time_range

    response = requests.get(f"{base_url}/search", params=params, timeout=args.timeout)
    response.raise_for_status()
    return {
        "request_url": response.url,
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type"),
        "data": response.json(),
    }


def normalize_result(item: dict[str, Any]) -> dict[str, Any]:
    url = str(item.get("url") or "")
    normalized_url = canonical_url(url) if url else ""
    published = item.get("publishedDate") or item.get("pubdate")
    return {
        "title": item.get("title") or "",
        "url": url,
        "canonical_url": normalized_url,
        "content": item.get("content") or "",
        "source": urlsplit(url).netloc.lower() if url else "",
        "engine": item.get("engine"),
        "engines": item.get("engines") or [],
        "category": item.get("category"),
        "score": item.get("score"),
        "published_at": published,
        "published_at_parsed": parse_datetime(published).isoformat() if parse_datetime(published) else None,
        "content_hash": hashlib.sha256(((item.get("title") or "") + "\n" + url).encode("utf-8")).hexdigest(),
    }


def select_results(raw_results: list[dict[str, Any]], max_articles: int, max_age_days: Optional[int]) -> list[dict[str, Any]]:
    seen_urls: set[str] = set()
    selected: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for item in raw_results:
        normalized = normalize_result(item)
        if not normalized["url"] or not normalized["title"]:
            continue
        if normalized["canonical_url"] in seen_urls:
            continue

        published = parse_datetime(normalized["published_at"])
        if max_age_days:
            # Undated results are section hubs, index pages, or evergreen
            # content. They cannot satisfy an age rule and would repeat
            # across runs, so they are discarded whenever an age rule is set.
            if published is None:
                continue
            age_days = (now - published).total_seconds() / 86400
            if age_days > max_age_days:
                continue

        seen_urls.add(normalized["canonical_url"])
        selected.append(normalized)
        if len(selected) >= max_articles:
            break

    return selected


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def source_rank_settings(args: Any) -> dict[str, Any]:
    """Resolve source-ranking settings from CLI args (when present) or NEWS_SOURCE_RANK_* env."""
    query = (getattr(args, "rank_query", None) or os.environ.get("NEWS_SOURCE_RANK_QUERY", "")).strip()
    candidates = getattr(args, "rank_candidates", None)
    if candidates is None:
        candidates = int(os.environ.get("NEWS_SOURCE_RANK_CANDIDATES", "50"))
    enabled = env_flag("NEWS_SOURCE_RANK_ENABLED", False) and bool(query)
    return {"enabled": enabled, "query": query, "candidates": max(int(candidates), 1)}


def rank_sources(query: str, sources: list[dict[str, Any]], top_n: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Score candidate sources against `query` with the NEWS_CONTEXT_RERANK_* endpoint.

    Posts title plus snippet for each source and returns the top_n sources in
    descending score order, each with a `source_rank_score` field added.
    """
    endpoint = os.environ.get("NEWS_CONTEXT_RERANK_ENDPOINT", "").strip()
    model = os.environ.get("NEWS_CONTEXT_RERANK_MODEL", "").strip()
    if not endpoint:
        raise RuntimeError("Set NEWS_CONTEXT_RERANK_ENDPOINT to rank sources.")

    requests = importlib.import_module("requests")
    documents = [f"{item.get('title') or ''}. {item.get('content') or ''}".strip()[:1000] for item in sources]
    payload: dict[str, Any] = {"query": query, "documents": documents, "top_n": len(documents)}
    if model:
        payload["model"] = model
    timeout = int(os.environ.get("NEWS_CONTEXT_RERANK_TIMEOUT_SECONDS", "60"))
    response = requests.post(endpoint, json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    results = (data.get("results") or data.get("data") or data.get("rankings") or []) if isinstance(data, dict) else data

    ranked: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        index = item.get("index", item.get("document_index"))
        score = item.get("relevance_score", item.get("score"))
        try:
            index = int(index)
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= len(sources):
            continue
        entry = dict(sources[index])
        entry["source_rank_score"] = None if score is None else float(score)
        ranked.append(entry)
    ranked.sort(key=lambda item: -1.0 if item["source_rank_score"] is None else item["source_rank_score"], reverse=True)
    selected = ranked[:top_n]
    return selected, {
        "enabled": True,
        "endpoint": endpoint,
        "model": model,
        "query": query,
        "candidate_count": len(sources),
        "returned_count": len(selected),
    }


def select_and_rank_sources(raw_results: list[dict[str, Any]], args: Any, limit: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select up to NEWS_SOURCE_RANK_CANDIDATES sources (age rule applied) and rank them.

    With limit=True (preview, CLI) the top max_articles are returned. With limit=False the whole
    ranked pool is returned, so the pipeline can walk down it until it has max_articles usable ones.
    Falls back to search order if ranking is disabled or the rerank call fails.
    """
    settings = source_rank_settings(args)
    pool_size = max(settings["candidates"], args.max_articles)
    if not settings["enabled"]:
        selected = select_results(raw_results, args.max_articles if limit else pool_size, args.max_age_days)
        return selected, {"enabled": False, "candidate_count": len(selected), "returned_count": len(selected)}

    candidates = select_results(raw_results, pool_size, args.max_age_days)
    if not candidates:
        return [], {"enabled": True, "query": settings["query"], "candidate_count": 0, "returned_count": 0}
    try:
        return rank_sources(settings["query"], candidates, args.max_articles if limit else len(candidates))
    except Exception as exc:  # degrade to search order rather than fail the run
        selected = candidates[:args.max_articles] if limit else candidates
        return selected, {
            "enabled": True,
            "query": settings["query"],
            "candidate_count": len(candidates),
            "returned_count": len(selected),
            "degraded": True,
            "error": f"{type(exc).__name__}: {exc}",
        }


def default_output_path(query: str) -> Path:
    output_root = Path(os.environ.get("NEWS_OUTPUT_DIRECTORY", PROJECT_ROOT / "output")).expanduser()
    if not output_root.is_absolute():
        output_root = PROJECT_ROOT / output_root
    date_dir = datetime.now().strftime("%Y-%m-%d")
    return output_root / date_dir / slugify(query) / "sources.json"


def write_bundle(path: Path, bundle: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search SearXNG and save selected source results.")
    search_group = parser.add_mutually_exclusive_group(required=True)
    search_group.add_argument("--subject", help="Search subject or keyword phrase.")
    search_group.add_argument("--search-text", help="Generic search text or keyword phrase.")
    parser.add_argument("--max-articles", type=int, default=int(os.environ.get("SEARXNG_DEFAULT_MAX_ARTICLES", "5")), help="Maximum selected sources to keep.")
    parser.add_argument("--max-age-days", type=int, default=int(os.environ.get("SEARXNG_DEFAULT_MAX_AGE_DAYS", "3")), help="Maximum article age when published dates are available.")
    parser.add_argument("--categories", help="SearXNG categories, e.g. news,general.")
    parser.add_argument("--language", help="SearXNG language code.")
    parser.add_argument("--safesearch", help="SearXNG safesearch value.")
    parser.add_argument("--time-range", choices=["day", "month", "year"], default=os.environ.get("SEARXNG_DEFAULT_TIME_RANGE") or None, help="Explicit SearXNG time_range.")
    parser.add_argument("--rank-query", help="Rank candidate sources against this text via the rerank endpoint. Defaults to NEWS_SOURCE_RANK_QUERY.")
    parser.add_argument("--rank-candidates", type=int, help="Candidate pool size before ranking. Defaults to NEWS_SOURCE_RANK_CANDIDATES.")
    parser.add_argument("--output", help="Output JSON path. Defaults under NEWS_OUTPUT_DIRECTORY.")
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("SEARXNG_DEFAULT_TIMEOUT_SECONDS", "20")), help="HTTP timeout in seconds.")
    return parser.parse_args()


def main() -> None:
    load_env_file(PROJECT_ROOT / ".env")
    args = parse_args()
    search_text = args.subject or args.search_text
    result = search_searxng(search_text, args)
    data = result["data"]
    raw_results = data.get("results") or []
    selected, source_rank = select_and_rank_sources(raw_results, args)

    output_path = Path(args.output).expanduser() if args.output else default_output_path(search_text)
    if not output_path.is_absolute():
        output_path = PROJECT_ROOT / output_path

    bundle = {
        "subject": search_text,
        "search_text": search_text,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "request_url": result["request_url"],
        "content_type": result["content_type"],
        "categories": args.categories or os.environ.get("SEARXNG_DEFAULT_CATEGORIES", ""),
        "language": args.language or os.environ.get("SEARXNG_DEFAULT_LANGUAGE", ""),
        "safesearch": args.safesearch or os.environ.get("SEARXNG_DEFAULT_SAFESEARCH", ""),
        "time_range": args.time_range or "",
        "max_articles": args.max_articles,
        "max_article_age_days": args.max_age_days,
        "raw_result_count": len(raw_results),
        "candidate_count": source_rank.get("candidate_count", len(selected)),
        "source_rank": source_rank,
        "selected_count": len(selected),
        "selected_sources": selected,
        "suggestions": data.get("suggestions") or [],
        "corrections": data.get("corrections") or [],
    }
    write_bundle(output_path, bundle)

    print(f"sources_path={output_path}")
    print(f"raw_result_count={len(raw_results)}")
    print(f"candidate_count={source_rank.get('candidate_count', len(selected))}")
    rank_line = f"source_rank_enabled={source_rank.get('enabled', False)}"
    if source_rank.get("query"):
        rank_line += f" query={source_rank['query']!r}"
    if source_rank.get("degraded"):
        rank_line += f" degraded={source_rank['error']}"
    print(rank_line)
    print(f"selected_count={len(selected)}")
    for index, source in enumerate(selected, 1):
        score = source.get("source_rank_score")
        score_text = "" if score is None else f" [{score:.4f}]"
        print(f"{index}.{score_text} {source['title']} | {source['url']}")


if __name__ == "__main__":
    main()
