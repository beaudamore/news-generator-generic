"""Complete-sentence evidence enrichment for script generation."""

from __future__ import annotations

import importlib
import os
import re
from typing import Any, Optional


_ABBREVIATION_MARKER = "<DOT>"
_ABBREVIATIONS = (
    "Mr.",
    "Mrs.",
    "Ms.",
    "Dr.",
    "Prof.",
    "Sr.",
    "Jr.",
    "St.",
    "Mt.",
    "U.S.",
    "U.K.",
    "U.N.",
    "E.U.",
    "D.C.",
    "Inc.",
    "Ltd.",
    "Co.",
    "Corp.",
    "Gov.",
    "Sen.",
    "Rep.",
    "Pres.",
    "Jan.",
    "Feb.",
    "Mar.",
    "Apr.",
    "Jun.",
    "Jul.",
    "Aug.",
    "Sep.",
    "Sept.",
    "Oct.",
    "Nov.",
    "Dec.",
    "a.m.",
    "p.m.",
    "e.g.",
    "i.e.",
    "vs.",
)


class EvidenceError(RuntimeError):
    pass


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    return int(value)


def protect_abbreviations(text: str) -> str:
    protected = text
    for abbreviation in _ABBREVIATIONS:
        protected = protected.replace(abbreviation, abbreviation.replace(".", _ABBREVIATION_MARKER))
    protected = re.sub(r"\b([A-Z])\.", lambda match: f"{match.group(1)}{_ABBREVIATION_MARKER}", protected)
    return protected


def restore_abbreviations(text: str) -> str:
    return text.replace(_ABBREVIATION_MARKER, ".")


def split_sentences(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    protected = protect_abbreviations(normalized)
    pieces = re.split(r"(?<=[.!?])\s+(?=[\"'“”‘’(]*[A-Z0-9])", protected)
    return [restore_abbreviations(piece).strip() for piece in pieces if piece.strip()]


def clean_sentence(sentence: str, min_chars: int) -> Optional[str]:
    cleaned = re.sub(r"\s+", " ", sentence).strip(" \t\r\n•-*–—")
    if len(cleaned) < min_chars:
        return None
    if "..." in cleaned or "…" in cleaned:
        return None
    if not re.search(r"[.!?][\"'”’)]*$", cleaned):
        return None
    if not re.search(r"[A-Za-z]", cleaned):
        return None
    low_value_patterns = (
        r"^subscribe\b",
        r"^sign up\b",
        r"^advertisement\b",
        r"^read more\b",
        r"^cookie\b",
        r"^privacy policy\b",
    )
    if any(re.search(pattern, cleaned, re.IGNORECASE) for pattern in low_value_patterns):
        return None
    return cleaned


def source_text(source: dict[str, Any]) -> str:
    return str(source.get("content") or "").strip()


def build_sentence_windows(sentences: list[str], window_sentences: int) -> list[str]:
    if window_sentences <= 0:
        return sentences
    units: list[str] = []
    for index in range(len(sentences)):
        start = max(0, index - window_sentences)
        end = min(len(sentences), index + window_sentences + 1)
        units.append(" ".join(sentences[start:end]))
    return units


# ----------------------------------------------------------------------------- story clustering

# Function words that say nothing about which story an article covers.
STORY_STOPWORDS = frozenset(
    "the a an and or of to in on for with by at from as is are was were be been being that this these those it its "
    "their his her they he she we you our your not but have has had will would can could should may might said says "
    "say also more than into over after before about which who whom whose what when where while new one two three "
    "first last year years day days week weeks month months today yesterday tomorrow just like get got make made "
    "there here then than them very much many most some any all each other such only own same so too out up down "
    "off again further once during between through under above below both because until against".split()
)


def story_enabled() -> bool:
    return env_bool("NEWS_STORY_DEDUPE_ENABLED", True)


def story_threshold() -> float:
    try:
        return float(os.environ.get("NEWS_STORY_DEDUPE_THRESHOLD", "0.35"))
    except ValueError:
        return 0.35


def story_vector(source: dict[str, Any]) -> dict[str, float]:
    """Unit term-frequency vector of an article's title and text, stopwords and short tokens removed."""
    counts: dict[str, int] = {}
    for word in tokenize(f"{source.get('title') or ''} {source_text(source) or source.get('snippet') or ''}"):
        if len(word) > 2 and word not in STORY_STOPWORDS:
            counts[word] = counts.get(word, 0) + 1
    norm = sum(v * v for v in counts.values()) ** 0.5 or 1.0
    return {k: v / norm for k, v in counts.items()}


def story_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(value * b.get(key, 0.0) for key, value in a.items())


def same_story(source: dict[str, Any], keepers: list[dict[str, Any]], threshold: float) -> Optional[tuple[int, float]]:
    """Index of the kept article this one duplicates (and the similarity), or None when it is a new story.

    Measured on a 10-10 run: three outlets on one story scored 0.45 to 0.64 against each other; every
    pair of different stories scored 0.21 or less. The default threshold 0.35 sits in that gap.
    Toggle: NEWS_STORY_DEDUPE_ENABLED; threshold: NEWS_STORY_DEDUPE_THRESHOLD.
    """
    vector = story_vector(source)
    best: Optional[tuple[int, float]] = None
    for index, keeper in enumerate(keepers):
        score = story_similarity(vector, keeper.get("_story_vector") or story_vector(keeper))
        if score >= threshold and (best is None or score > best[1]):
            best = (index, score)
    return best


def collect_evidence_units(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    min_chars = env_int("NEWS_CONTEXT_MIN_SENTENCE_CHARS", 40)
    window_sentences = env_int("NEWS_CONTEXT_WINDOW_SENTENCES", 0)
    max_sentences = env_int("NEWS_CONTEXT_MAX_SENTENCES", 80)
    evidence: list[dict[str, Any]] = []
    sources = bundle.get("selected_sources") or []
    # Articles that cover the same story as a selected one (see same_story) still contribute
    # sentences; each unit says which story it belongs to so the writer tells it once.
    duplicates = bundle.get("duplicate_sources") or []
    stories = [(index + 1, source) for index, source in enumerate(sources)]
    for duplicate in duplicates:
        keeper_url = duplicate.get("duplicate_of")
        story = next((number for number, source in stories if source.get("url") == keeper_url), None)
        stories.append((story, duplicate))
    # With full-article extraction one long article would use the whole budget; share it across articles.
    per_source = max(max_sentences // max(len(stories), 1), 4)

    for source_index, (story, source) in enumerate(stories):
        sentences = [
            cleaned
            for sentence in split_sentences(source_text(source))
            if (cleaned := clean_sentence(sentence, min_chars))
        ]
        windows = build_sentence_windows(sentences, window_sentences)[:per_source]
        others = [d.get("source") for d in source.get("also_reported_by") or [] if isinstance(d, dict) and d.get("source")]
        for sentence_index, text in enumerate(windows):
            unit = {
                "text": text,
                "source_index": source_index,
                "sentence_index": sentence_index,
                "title": source.get("title") or "",
                "url": source.get("url") or "",
                "source": source.get("source") or "",
            }
            if story is not None and (duplicates or len(stories) != len(sources)):
                unit["story"] = story
            if others:
                unit["also_reported_by"] = list(others)
            evidence.append(unit)
            if len(evidence) >= max_sentences:
                return evidence
    return evidence


def normalize_rerank_results(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        results = data.get("results") or data.get("data") or data.get("rankings") or []
    else:
        results = data
    if not isinstance(results, list):
        raise EvidenceError("Rerank response must contain a list of results.")

    normalized: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if index is None:
            index = item.get("document_index")
        if index is None:
            index = item.get("corpus_id")
        if index is None:
            continue
        score = item.get("score")
        if score is None:
            score = item.get("relevance_score")
        try:
            normalized.append({"index": int(index), "score": None if score is None else float(score)})
        except (TypeError, ValueError):
            continue
    return normalized


_WORD = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def rerank_lexical(
    search_text: str, evidence: list[dict[str, Any]], top_n: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rank evidence with in-process BM25. No service, no model, no new deps.

    Weakest of the three backends on paraphrase, since it matches words rather
    than meaning, but it is deterministic, instant, and always available. Used
    directly when NEWS_CONTEXT_RERANK_BACKEND=lexical, and as the fallback when
    a configured backend is unreachable, so evidence is never left unranked.
    """
    k1, b = 1.5, 0.75
    docs = [tokenize(str(item.get("text") or "")) for item in evidence]
    query = tokenize(search_text)

    lengths = [len(d) for d in docs]
    avg_len = (sum(lengths) / len(lengths)) if lengths else 0.0
    total = len(docs)

    # Document frequency per query term, over this candidate set only.
    doc_freq: dict[str, int] = {}
    for term in set(query):
        doc_freq[term] = sum(1 for d in docs if term in d)

    import math

    scores: list[float] = []
    for doc, length in zip(docs, lengths):
        score = 0.0
        for term in query:
            freq = doc.count(term)
            if freq == 0:
                continue
            df = doc_freq.get(term, 0)
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            norm = freq * (k1 + 1) / (freq + k1 * (1 - b + b * (length / avg_len if avg_len else 1.0)))
            score += idf * norm
        scores.append(score)

    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    low, high = (min(scores), max(scores)) if scores else (0.0, 0.0)
    span = high - low

    selected: list[dict[str, Any]] = []
    for index in ranked[:top_n]:
        item = dict(evidence[index])
        item["rerank_score"] = 1.0 if span == 0 else (scores[index] - low) / span
        selected.append(item)

    return selected, {
        "enabled": True,
        "backend": "lexical",
        "algorithm": "bm25",
        "top_n": top_n,
        "returned_count": len(selected),
    }


def ollama_base_url() -> str:
    """Base URL for the built-in Ollama rerank backend.

    Falls back to OPENAI_BASE_URL with the OpenAI-compat /v1 suffix stripped,
    since the script LLM usually already points at the same Ollama server.
    """
    base = os.environ.get("NEWS_CONTEXT_RERANK_BASE_URL", "").strip()
    if not base:
        base = os.environ.get("OPENAI_BASE_URL", "").strip()
        if base.rstrip("/").endswith("/v1"):
            base = base.rstrip("/")[: -len("/v1")]
    if not base:
        raise EvidenceError(
            "Set NEWS_CONTEXT_RERANK_BASE_URL (or OPENAI_BASE_URL) for the ollama-embed rerank backend."
        )
    return base.rstrip("/")


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def rerank_via_ollama_embeddings(
    search_text: str, evidence: list[dict[str, Any]], top_n: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rank evidence by embedding similarity using Ollama directly.

    Ollama exposes no cross-encoder rerank route, so this embeds the query and
    each evidence unit, then ranks by cosine similarity normalized to 0..1. It
    is genuine semantic ranking but weaker than a true cross-encoder; point
    NEWS_CONTEXT_RERANK_MODEL at an embedding model, not a BGE reranker.
    """
    model = os.environ.get("NEWS_CONTEXT_RERANK_MODEL", "").strip()
    if not model:
        raise EvidenceError("Set NEWS_CONTEXT_RERANK_MODEL to an embedding model for the ollama-embed backend.")

    base_url = ollama_base_url()
    timeout = env_int("NEWS_CONTEXT_RERANK_TIMEOUT_SECONDS", 60)
    requests = importlib.import_module("requests")

    texts = [search_text] + [str(item.get("text") or "") for item in evidence]
    response = requests.post(
        f"{base_url}/api/embed",
        json={"model": model, "input": texts},
        timeout=timeout,
    )
    response.raise_for_status()
    vectors = response.json().get("embeddings") or []
    if len(vectors) != len(texts):
        raise EvidenceError(
            f"Embedding backend returned {len(vectors)} vectors for {len(texts)} inputs."
        )

    query_vector, doc_vectors = vectors[0], vectors[1:]
    scored = [(index, cosine_similarity(query_vector, vec)) for index, vec in enumerate(doc_vectors)]

    # Normalize to 0..1 across this batch so scores read consistently.
    raw = [score for _, score in scored]
    low, high = min(raw), max(raw)
    span = high - low

    scored.sort(key=lambda pair: pair[1], reverse=True)
    selected: list[dict[str, Any]] = []
    for index, score in scored[:top_n]:
        item = dict(evidence[index])
        item["rerank_score"] = 1.0 if span == 0 else (score - low) / span
        selected.append(item)

    return selected, {
        "enabled": True,
        "backend": "ollama-embed",
        "endpoint": f"{base_url}/api/embed",
        "model": model,
        "top_n": top_n,
        "returned_count": len(selected),
    }


def rerank_evidence(search_text: str, evidence: list[dict[str, Any]], top_n: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    endpoint = os.environ.get("NEWS_CONTEXT_RERANK_ENDPOINT", "").strip()
    model = os.environ.get("NEWS_CONTEXT_RERANK_MODEL", "").strip()
    if not endpoint:
        raise EvidenceError("Set NEWS_CONTEXT_RERANK_ENDPOINT or disable NEWS_CONTEXT_RERANK_ENABLED.")

    requests = importlib.import_module("requests")
    payload: dict[str, Any] = {
        "query": search_text,
        "documents": [item["text"] for item in evidence],
        "top_n": top_n,
    }
    if model:
        payload["model"] = model

    timeout = env_int("NEWS_CONTEXT_RERANK_TIMEOUT_SECONDS", 60)
    response = requests.post(endpoint, json=payload, timeout=timeout)
    response.raise_for_status()
    results = normalize_rerank_results(response.json())

    selected: list[dict[str, Any]] = []
    for result in results[:top_n]:
        index = result["index"]
        if index < 0 or index >= len(evidence):
            continue
        item = dict(evidence[index])
        item["rerank_score"] = result.get("score")
        selected.append(item)

    return selected, {
        "enabled": True,
        "endpoint": endpoint,
        "model": model,
        "top_n": top_n,
        "returned_count": len(selected),
    }


# ----------------------------------------------------------------------------- near-duplicate merge


def dedupe_enabled() -> bool:
    return env_bool("NEWS_CONTEXT_DEDUPE_ENABLED", True)


def dedupe_threshold() -> float:
    try:
        return float(os.environ.get("NEWS_CONTEXT_DEDUPE_THRESHOLD", "0.6"))
    except ValueError:
        return 0.6


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedupe_evidence(selected: list[dict[str, Any]], threshold: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop evidence units that repeat an earlier (higher-ranked) one; the kept unit remembers the other outlets.

    Similarity is word-set Jaccard, so wire copy and light rewrites merge while different facts
    from the same story stay apart. A dropped unit's outlet (when it differs from the keeper's)
    is appended to the keeper's `also_reported_by`, which the prompt renders as
    "Also reported by: ..." so the writer credits the outlets together instead of reading the
    fact twice. Toggle: NEWS_CONTEXT_DEDUPE_ENABLED; threshold: NEWS_CONTEXT_DEDUPE_THRESHOLD.
    """
    kept: list[dict[str, Any]] = []
    kept_tokens: list[set[str]] = []
    dropped = 0
    merged: list[dict[str, Any]] = []
    for item in selected:
        tokens = set(tokenize(str(item.get("text") or "")))
        match = None
        for index, other in enumerate(kept_tokens):
            if jaccard(tokens, other) >= threshold:
                match = index
                break
        if match is None:
            kept.append(dict(item))
            kept_tokens.append(tokens)
            continue
        dropped += 1
        keeper = kept[match]
        source = str(item.get("source") or "")
        also = keeper.setdefault("also_reported_by", [])
        if source and source != str(keeper.get("source") or "") and source not in also:
            also.append(source)
        merged.append(
            {
                "kept": str(keeper.get("source") or ""),
                "dropped": source,
                "similarity": round(jaccard(tokens, kept_tokens[match]), 3),
            }
        )
    return kept, {"enabled": True, "threshold": threshold, "dropped": dropped, "merged": merged}


def enrich_source_bundle(bundle: dict[str, Any], rerank_top_n: Optional[int] = None) -> dict[str, Any]:
    evidence = collect_evidence_units(bundle)
    top_n = rerank_top_n if rerank_top_n is not None else env_int("NEWS_CONTEXT_RERANK_TOP_N", 20)
    if top_n <= 0:
        raise EvidenceError("rerank_top_n must be greater than zero.")
    # With the near-duplicate merge on, rank a larger pool so top_n is still full after merging.
    dedupe = dedupe_enabled()
    pool = min(len(evidence), top_n * 2) if dedupe else top_n

    context: dict[str, Any] = {
        "mode": "complete-sentences",
        "rerank": {"enabled": False, "top_n": top_n},
        "candidate_count": len(evidence),
        "selected_count": min(len(evidence), top_n),
        "evidence_units": evidence[:pool],
    }
    if env_bool("NEWS_CONTEXT_RERANK_ENABLED", False) and evidence:
        search_text = str(bundle.get("search_text") or bundle.get("subject") or "")
        backend = os.environ.get("NEWS_CONTEXT_RERANK_BACKEND", "http").strip().lower()
        try:
            if backend == "lexical":
                selected, rerank_info = rerank_lexical(search_text, evidence, pool)
            elif backend in ("ollama", "ollama-embed"):
                selected, rerank_info = rerank_via_ollama_embeddings(search_text, evidence, pool)
            else:
                selected, rerank_info = rerank_evidence(search_text, evidence, pool)
        except Exception as exc:
            # An unreachable or misbehaving backend must not kill the run.
            # Fall back to in-process lexical ranking rather than leaving the
            # evidence in whatever order search returned it, and record why.
            selected, rerank_info = rerank_lexical(search_text, evidence, pool)
            rerank_info["degraded_from"] = backend
            rerank_info["error"] = f"{type(exc).__name__}: {exc}"
            context["rerank"] = rerank_info
            context["selected_count"] = len(selected)
            context["evidence_units"] = selected
        else:
            context["rerank"] = rerank_info
            context["selected_count"] = len(selected)
            context["evidence_units"] = selected

    if dedupe and context["evidence_units"]:
        deduped, dedupe_info = dedupe_evidence(context["evidence_units"], dedupe_threshold())
        dedupe_info.update({"pool": pool, "top_n": top_n})
        context["dedupe"] = dedupe_info
        context["evidence_units"] = deduped[:top_n]
    else:
        context["dedupe"] = {"enabled": False}
        context["evidence_units"] = context["evidence_units"][:top_n]
    context["selected_count"] = len(context["evidence_units"])

    enriched = dict(bundle)
    enriched["evidence_context"] = context
    return enriched
