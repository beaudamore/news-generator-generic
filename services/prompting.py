"""Prompt templating and evidence context for script generation.

Lives in the service layer so the FastAPI pipeline and the CLI helpers share
one implementation. Depends on nothing under scripts/ to keep imports acyclic.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any, Optional


_PLACEHOLDER = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*\}\}")

_OUTLET_NAMES = {
    "msn": "MSN", "cnn": "CNN", "bbc": "BBC", "npr": "NPR", "cnbc": "CNBC", "abc": "ABC News", "nbc": "NBC News",
    "cbs": "CBS News", "digitimes": "DigiTimes", "reuters": "Reuters", "apnews": "The Associated Press",
    "nytimes": "The New York Times", "wsj": "The Wall Street Journal", "ft": "The Financial Times",
    "theverge": "The Verge", "techcrunch": "TechCrunch", "arstechnica": "Ars Technica", "wired": "Wired",
    "huffpost": "HuffPost", "aol": "AOL", "yahoo": "Yahoo", "bloomberg": "Bloomberg", "ansa": "ANSA",
    "theguardian": "The Guardian", "dailystar": "The Daily Star", "space": "Space.com", "usatoday": "USA Today",
    "usnews": "U.S. News", "foxnews": "Fox News", "forbes": "Forbes", "axios": "Axios", "politico": "Politico",
    "businessinsider": "Business Insider", "engadget": "Engadget", "zdnet": "ZDNet", "venturebeat": "VentureBeat",
}


def outlet_name(domain: str) -> str:
    """Human-readable outlet name from a source domain, e.g. finance.yahoo.com -> Yahoo Finance."""
    host = (domain or "").strip().lower()
    if not host:
        return ""
    labels = [part for part in host.split(".") if part and part not in ("www", "m", "amp", "edition")]
    if len(labels) >= 2:
        second_level = labels[-2]
        prefix = labels[:-2]
    else:
        second_level, prefix = labels[0], []
    name = _OUTLET_NAMES.get(second_level, second_level.replace("-", " ").title())
    if second_level == "yahoo" and "finance" in prefix:
        return "Yahoo Finance"
    return name


def source_lines(sources: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, source in enumerate(sources, 1):
        lines.append(
            f"[{index}] {source.get('title', '').strip()}\n"
            f"URL: {source.get('url', '').strip()}\n"
            f"Source: {source.get('source', '').strip()}\n"
            f"Outlet: {outlet_name(source.get('source', ''))}\n"
            f"Summary: {source.get('content', '').strip()}"
        )
    return "\n\n".join(lines)


def evidence_lines(evidence_units: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, item in enumerate(evidence_units, 1):
        score = item.get("rerank_score")
        score_text = "" if score is None else f"\nRerank score: {score:.4f}"
        also = [outlet_name(str(domain)) for domain in item.get("also_reported_by") or [] if domain]
        also_text = "" if not also else f"\nAlso reported by: {', '.join(also)}"
        story = item.get("story")
        story_text = "" if story is None else f"\nStory: {story}"
        lines.append(
            f"[{index}] {str(item.get('title') or '').strip()}\n"
            f"URL: {str(item.get('url') or '').strip()}\n"
            f"Source: {str(item.get('source') or '').strip()}\n"
            f"Outlet: {outlet_name(str(item.get('source') or ''))}{story_text}{also_text}{score_text}\n"
            f"Evidence: {str(item.get('text') or '').strip()}"
        )
    return "\n\n".join(lines)


def env_default(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def prompt_variables(
    bundle: dict[str, Any],
    args: Any,
    intended_seconds: Optional[float],
    target_words: Optional[int],
) -> dict[str, str]:
    now = datetime.now().astimezone()
    categories = str(
        getattr(args, "categories", None)
        or bundle.get("categories")
        or env_default("SEARXNG_DEFAULT_CATEGORIES")
    )
    category = categories.split(",", 1)[0].strip() if categories else ""
    language = str(
        getattr(args, "language", None)
        or bundle.get("language")
        or env_default("SEARXNG_DEFAULT_LANGUAGE")
    )
    safesearch = str(
        getattr(args, "safesearch", None)
        or bundle.get("safesearch")
        or env_default("SEARXNG_DEFAULT_SAFESEARCH")
    )
    time_range = str(
        getattr(args, "time_range", None)
        or bundle.get("time_range")
        or env_default("SEARXNG_DEFAULT_TIME_RANGE")
    )
    selected_sources = bundle.get("selected_sources") or []
    wpm = getattr(args, "wpm", None)
    return {
        "CATEGORY": category,
        "CATEGORIES": categories,
        "CURRENT_DATE": now.strftime("%Y-%m-%d"),
        "CURRENT_DATETIME": now.isoformat(timespec="seconds"),
        "CURRENT_TIME": now.strftime("%H:%M:%S"),
        "CURRENT_TIMEZONE": now.tzname() or "",
        "CURRENT_WEEKDAY": now.strftime("%A"),
        "INTENDED_VIDEO_MAX_SECONDS": "" if intended_seconds is None else f"{intended_seconds:g}",
        "LANGUAGE": language,
        "MAX_AGE_DAYS": str(bundle.get("max_article_age_days") or getattr(args, "max_age_days", "") or ""),
        "MAX_ARTICLES": str(bundle.get("max_articles") or getattr(args, "max_articles", "") or ""),
        "RAW_RESULT_COUNT": str(bundle.get("raw_result_count") or 0),
        "SAFESEARCH": safesearch,
        "SEARCH_TEXT": str(bundle.get("search_text") or ""),
        "SELECTED_SOURCE_COUNT": str(bundle.get("selected_count") or len(selected_sources)),
        "SOURCE_COUNT": str(bundle.get("selected_count") or len(selected_sources)),
        "SUBJECT": str(bundle.get("subject") or ""),
        "TARGET_WORDS": "" if target_words is None else str(target_words),
        "TIME_RANGE": time_range,
        "WPM": f"{wpm:g}" if isinstance(wpm, (int, float)) else str(wpm or ""),
    }


def render_prompt_variables(prompt: str, variables: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        return variables.get(name, match.group(0))

    return _PLACEHOLDER.sub(replace, prompt)


def build_prompt(bundle: dict[str, Any], summarize_prompt: str) -> tuple[str, str]:
    """Build the (system, user) prompt pair, preferring reranked evidence."""
    subject = bundle.get("subject") or "the requested topic"
    sources = bundle.get("selected_sources") or []
    evidence_context = bundle.get("evidence_context") or {}
    evidence_units = evidence_context.get("evidence_units") or []
    context_label = "Complete-sentence evidence" if evidence_units else "Sources"
    context_lines = evidence_lines(evidence_units) if evidence_units else source_lines(sources)
    user_prompt = f"""Subject: {subject}

{context_label}:
{context_lines}

Write one polished news-anchor script. Requirements:
- Use only facts supported by the source snippets.
- Start every story with an attribution to its Outlet, using a different phrasing each time (According to X, ... / X reports ... / From X today, ... / X is saying ...).
- Evidence lines that share a Story number are one story covered by different outlets. Tell that story once, drawing on all of its lines, and credit its outlets together (Both X and Y report ... / X and Y both say ...). Do the same when a line says "Also reported by". Name every Outlet whose material you use, but never repeat a fact just to name another outlet.
- Keep a neutral broadcast tone.
- Output only the script text.
"""
    return summarize_prompt, user_prompt
