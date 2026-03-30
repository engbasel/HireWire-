"""
HireWire — AI-Powered Data Extraction Engine

Replaces ALL CSS selectors with semantic LLM-based extraction.
When a platform changes its UI, this module keeps working because it
understands the *meaning* of text, not its HTML structure.

Architecture:
    Playwright → raw page text → Gemini 2.5 Flash → structured JSON

Includes built-in rate limiting (4 RPM) and exponential backoff retry
for Gemini free-tier quota (5 RPM limit).
"""

import json
import re
import time
import threading
from typing import Any

from google import genai

from config import GEMINI_API_KEY, GEMINI_MODEL, logger
from models import Project, ClientInfo


# ---------------------------------------------------------------------------
# Rate Limiter — stay safely under free-tier 5 RPM
# ---------------------------------------------------------------------------
_MAX_REQUESTS_PER_MINUTE = 4  # Safe margin under 5 RPM free tier
_MIN_INTERVAL = 60.0 / _MAX_REQUESTS_PER_MINUTE  # ~15 seconds between calls
_last_request_time: float = 0.0
_rate_lock = threading.Lock()


def _rate_limit_wait() -> None:
    """Block until enough time has passed since the last API call."""
    global _last_request_time
    with _rate_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < _MIN_INTERVAL:
            wait_time = _MIN_INTERVAL - elapsed
            logger.debug("[Rate Limiter] Waiting %.1fs before next API call", wait_time)
            time.sleep(wait_time)
        _last_request_time = time.time()


# ---------------------------------------------------------------------------
# Gemini Client (lazy singleton)
# ---------------------------------------------------------------------------
_client: genai.Client | None = None


def _get_client() -> genai.Client:
    """Lazy-initialize the Gemini client."""
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


# ---------------------------------------------------------------------------
# Retry-Aware API Call
# ---------------------------------------------------------------------------
_MAX_RETRIES = 3
_BASE_BACKOFF = 20.0  # seconds


def _call_gemini(prompt: str, label: str = "request") -> str | None:
    """
    Call Gemini with rate limiting and exponential backoff on 429.
    Returns the response text or None on failure.
    """
    client = _get_client()

    for attempt in range(1, _MAX_RETRIES + 1):
        _rate_limit_wait()

        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            return response.text

        except Exception as exc:
            error_str = str(exc)

            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                # Parse retry delay from error if available
                retry_match = re.search(r"retryDelay.*?(\d+)", error_str)
                wait_secs = float(retry_match.group(1)) if retry_match else _BASE_BACKOFF * attempt

                # Cap at 120 seconds
                wait_secs = min(wait_secs + 5, 120.0)

                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "[AI Extractor] 429 Rate Limited (%s) — retry %d/%d in %.0fs",
                        label, attempt, _MAX_RETRIES, wait_secs,
                    )
                    time.sleep(wait_secs)
                    continue
                else:
                    logger.error(
                        "[AI Extractor] 429 Rate Limited (%s) — all %d retries exhausted",
                        label, _MAX_RETRIES,
                    )
                    return None
            else:
                logger.error("[AI Extractor] %s failed: %s", label, error_str[:200])
                return None

    return None


# ---------------------------------------------------------------------------
# Extraction Prompts
# ---------------------------------------------------------------------------
_LISTING_PROMPT = """You are a structured data extraction engine.
Extract ALL project/job listings visible on this freelancing platform page.

Return a JSON array. Each element MUST have exactly these fields:
{{
  "title": "the project or job title (string)",
  "url": "the full URL or path to the project detail page (string)"
}}

Rules:
- Extract ONLY real project/job listings. Ignore navigation links, category links, and ads.
- If a URL is relative (starts with /), keep it as-is — do NOT invent a domain.
- Return an empty array [] if no projects are found.
- Do NOT add commentary — return ONLY the JSON array.

Platform: {platform}

Page text:
---
{page_text}
---"""

_PROJECT_PROMPT = """You are a structured data extraction engine for freelancing platforms.
Extract project details from this single project/job page.

Return a single JSON object with EXACTLY these fields:
{{
  "title": "project title (string)",
  "description": "project description, max 800 chars (string)",
  "budget": "budget as displayed, e.g. '$500', '£45/hr', '$25-50' (string, empty if not found)",
  "client_name": "client or project owner name (string, empty if not found)",
  "hiring_rate": -99,
  "total_projects": 0,
  "country": "client country if visible (string, empty if not found)",
  "skills": ["list", "of", "skills/tags"],
  "proposals_count": "number of proposals/offers/quotes (string, empty if not found)",
  "time_posted": "when the project was posted (string, empty if not found)"
}}

Platform-specific hiring rate rules:
- Mostaql (مستقل): Look for "معدل التوظيف" followed by a percentage → extract the number (0-100). If this exact text is NOT present, set hiring_rate to -99.
- Nafezly (نفذلي): No hiring rate displayed → set to -99.
- PeoplePerHour: Look for "PROJECTS AWARDED" or "% Projects Awarded" → extract the number. If not present, set to -99.
- Guru: Look for "% Hired" or "Feedback" percentage → extract the number. If not present, set to -99.
- IMPORTANT: If hiring rate is genuinely not visible in the text, you MUST set to -99. Do NOT guess 0.

Rules:
- Extract ONLY what exists in the text. Do NOT invent or hallucinate data.
- For hiring_rate, return the INTEGER percentage (e.g. 63, not "63%").
- For budget, copy it exactly as shown (with currency symbol).
- If a field truly doesn't exist in the text, use empty string "" or 0 for numbers.

Platform: {platform}

Page text:
---
{page_text}
---"""


# ---------------------------------------------------------------------------
# Text Cleaning
# ---------------------------------------------------------------------------
def _clean_page_text(raw_text: str, max_chars: int = 6000) -> str:
    """
    Clean raw page text for LLM consumption:
    - Collapse excessive whitespace
    - Remove cookie banners and boilerplate
    - Truncate to max_chars
    """
    if not raw_text:
        return ""

    # Collapse 3+ consecutive newlines to 2
    text = re.sub(r"\n{3,}", "\n\n", raw_text)

    # Collapse runs of spaces/tabs
    text = re.sub(r"[ \t]{3,}", "  ", text)

    # Remove common cookie/consent banner patterns
    noise_patterns = [
        r"(?i)we use cookies.*?(?:accept|reject|manage|close)[\s.]*",
        r"(?i)this website uses cookies.*?(?:\n){1,3}",
        r"(?i)cookie\s*(?:policy|settings|preferences).*?\n",
    ]
    for pattern in noise_patterns:
        text = re.sub(pattern, "", text)

    return text.strip()[:max_chars]


# ---------------------------------------------------------------------------
# JSON Parsing (robust)
# ---------------------------------------------------------------------------
def _parse_json_response(text: str) -> Any:
    """
    Parse JSON from Gemini response, handling markdown code fences
    and minor formatting issues.
    """
    if not text:
        return None

    # Strip markdown code fences
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Remove opening ```json or ```
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find JSON array or object in the text
        array_match = re.search(r"(\[[\s\S]*\])", cleaned)
        if array_match:
            try:
                return json.loads(array_match.group(1))
            except json.JSONDecodeError:
                pass

        obj_match = re.search(r"(\{[\s\S]*\})", cleaned)
        if obj_match:
            try:
                return json.loads(obj_match.group(1))
            except json.JSONDecodeError:
                pass

    logger.warning("[AI Extractor] Could not parse JSON from response: %s", text[:200])
    return None


# ---------------------------------------------------------------------------
# Public API: Extract Listing Projects
# ---------------------------------------------------------------------------
def extract_listing_projects(
    page_text: str,
    platform: str,
    base_url: str = "",
) -> list[dict[str, str]]:
    """
    Extract project titles + URLs from a listing page using AI.

    Args:
        page_text: Raw text content from the listing page.
        platform:  Platform name ("mostaql", "nafezly", "pph", "guru").
        base_url:  Base URL to prepend to relative paths.

    Returns:
        List of dicts: [{"title": "...", "url": "..."}, ...]
    """
    cleaned = _clean_page_text(page_text, max_chars=8000)
    if not cleaned or len(cleaned) < 50:
        logger.warning("[AI Extractor] Page text too short for listing extraction (%d chars)", len(cleaned))
        return []

    prompt = _LISTING_PROMPT.format(platform=platform, page_text=cleaned)
    response_text = _call_gemini(prompt, label=f"{platform}-listing")

    if response_text is None:
        return []

    data = _parse_json_response(response_text)

    if not isinstance(data, list):
        logger.warning("[AI Extractor] Expected list, got %s", type(data))
        return []

    # Normalize URLs
    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for item in data:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        url = str(item.get("url", "")).strip()

        if not title or not url:
            continue

        # Prepend base_url for relative paths
        if not url.startswith("http") and base_url:
            url = base_url.rstrip("/") + "/" + url.lstrip("/")

        if url not in seen_urls:
            seen_urls.add(url)
            results.append({"title": title, "url": url})

    logger.info(
        "[AI Extractor] Extracted %d projects from %s listing page",
        len(results), platform,
    )
    return results


# ---------------------------------------------------------------------------
# Public API: Extract Project Details
# ---------------------------------------------------------------------------
def extract_project_details(
    page_text: str,
    platform: str,
    fallback_title: str = "",
    fallback_url: str = "",
) -> Project:
    """
    Extract full project details from a single project page using AI.

    This replaces hundreds of lines of CSS selectors with a single
    Gemini call that understands the page semantically.
    """
    cleaned = _clean_page_text(page_text, max_chars=6000)

    # If page text is too thin, return a minimal Project
    if not cleaned or len(cleaned) < 30:
        logger.warning("[AI Extractor] Page text too short for project extraction")
        return Project(title=fallback_title, url=fallback_url, source=platform)

    prompt = _PROJECT_PROMPT.format(platform=platform, page_text=cleaned)
    response_text = _call_gemini(prompt, label=f"{platform}-project")

    if response_text is None:
        return Project(title=fallback_title, url=fallback_url, source=platform)

    data = _parse_json_response(response_text)

    if not isinstance(data, dict):
        logger.warning("[AI Extractor] Expected dict, got %s", type(data))
        return Project(title=fallback_title, url=fallback_url, source=platform)

    # Build ClientInfo
    hiring_rate_raw = data.get("hiring_rate", -99)
    if isinstance(hiring_rate_raw, str):
        # Strip "%" and parse
        hiring_rate_raw = re.sub(r"[^\d.-]", "", hiring_rate_raw)
        try:
            hiring_rate_raw = int(float(hiring_rate_raw))
        except (ValueError, TypeError):
            hiring_rate_raw = -99

    client_info = ClientInfo(
        name=str(data.get("client_name", "")).strip(),
        hiring_rate=int(hiring_rate_raw) if hiring_rate_raw != -99 else _platform_default_rate(platform),
        total_projects=int(data.get("total_projects", 0)),
        country=str(data.get("country", "")).strip(),
    )

    # Build Project
    project = Project(
        title=str(data.get("title", fallback_title)).strip() or fallback_title,
        url=fallback_url,  # Always use the known URL
        description=str(data.get("description", "")).strip()[:1000],
        budget=str(data.get("budget", "")).strip(),
        time_posted=str(data.get("time_posted", "")).strip(),
        skills=data.get("skills", []) if isinstance(data.get("skills"), list) else [],
        proposals_count=str(data.get("proposals_count", "")).strip(),
        client=client_info,
        source=platform,
    )

    logger.info(
        "    ↳ [AI] 👤 %-15s | 📊 Rate: %s%% | 💰 %s | 🏷️ %s",
        (client_info.name or "Unknown")[:15],
        client_info.hiring_rate if client_info.hiring_rate >= 0 else "N/A",
        project.budget or "N/A",
        platform,
    )

    return project


def _platform_default_rate(platform: str) -> int:
    """Default hiring rate when AI returns -99 (not found on page).

    Returns -1 = "unknown/unavailable" (will NOT be filtered out).
    This ensures no platform wrongly drops projects just because
    the hiring rate field isn't shown on the page.
    """
    # All platforms: if AI can't find it, treat as unknown → pass through filter
    return -1
