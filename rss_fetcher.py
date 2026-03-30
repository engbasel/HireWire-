"""
HireWire — RSS Feed Fetcher

Fetches Mostaql projects via RSS feed instead of scraping.
This eliminates the need for Playwright on the listing page entirely.

Benefits:
    - Zero anti-bot risk (RSS is a public, stable API)
    - Zero selector maintenance (no CSS to break)
    - Faster than browser rendering (~200ms vs ~5s)
    - Never gets IP-blocked
"""

import re
import time
from typing import Any
from xml.etree import ElementTree

import requests

from config import logger


# ---------------------------------------------------------------------------
# RSS Parser (lightweight, no extra dependency needed)
# ---------------------------------------------------------------------------
def _parse_rss_xml(xml_text: str) -> list[dict[str, str]]:
    """
    Parse RSS XML into a list of items.
    Each item has: title, url (link), description, published.
    """
    items: list[dict[str, str]] = []

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        logger.error("[RSS] Failed to parse XML: %s", exc)
        return []

    # Standard RSS 2.0: /rss/channel/item
    for item_elem in root.findall(".//item"):
        title_el = item_elem.find("title")
        link_el = item_elem.find("link")
        desc_el = item_elem.find("description")
        pub_date_el = item_elem.find("pubDate")

        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        url = link_el.text.strip() if link_el is not None and link_el.text else ""
        description = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
        published = pub_date_el.text.strip() if pub_date_el is not None and pub_date_el.text else ""

        # Clean HTML tags from description
        if description:
            description = re.sub(r"<[^>]+>", "", description).strip()
            description = description[:500]

        if title and url:
            items.append({
                "title": title,
                "url": url,
                "description": description,
                "published": published,
            })

    return items


# ---------------------------------------------------------------------------
# Atom Parser (some feeds use Atom format)
# ---------------------------------------------------------------------------
def _parse_atom_xml(xml_text: str) -> list[dict[str, str]]:
    """Parse Atom XML format."""
    items: list[dict[str, str]] = []
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []

    for entry in root.findall(".//atom:entry", ns):
        title_el = entry.find("atom:title", ns)
        link_el = entry.find("atom:link", ns)
        summary_el = entry.find("atom:summary", ns)
        updated_el = entry.find("atom:updated", ns)

        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        url = link_el.get("href", "").strip() if link_el is not None else ""
        description = summary_el.text.strip() if summary_el is not None and summary_el.text else ""
        published = updated_el.text.strip() if updated_el is not None and updated_el.text else ""

        if description:
            description = re.sub(r"<[^>]+>", "", description).strip()[:500]

        if title and url:
            items.append({
                "title": title,
                "url": url,
                "description": description,
                "published": published,
            })

    return items


# ---------------------------------------------------------------------------
# Public API: Fetch Mostaql RSS
# ---------------------------------------------------------------------------
def fetch_mostaql_rss(
    category: str = "development",
    max_items: int = 30,
) -> list[dict[str, str]]:
    """
    Fetch Mostaql project listings via RSS/Atom feed.
    Eliminates the need for Playwright on the listing page.

    Args:
        category: Project category (default "development").
        max_items: Maximum items to return.

    Returns:
        List of dicts: [{"title": "...", "url": "...", "description": "...", "published": "..."}, ...]
        Returns empty list on failure (caller should fall back to Playwright).
    """
    # Mostaql RSS feed URLs to try (they may vary)
    feed_urls = [
        f"https://mostaql.com/projects/feed?category={category}",
        f"https://mostaql.com/projects.rss?category={category}",
        "https://mostaql.com/projects/feed",
        "https://mostaql.com/feed",
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; HireWire/1.0; Feed Reader)",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
    }

    for feed_url in feed_urls:
        try:
            logger.info("[RSS] Trying feed: %s", feed_url)
            response = requests.get(feed_url, headers=headers, timeout=15)

            if response.status_code != 200:
                logger.debug("[RSS] Got HTTP %d from %s", response.status_code, feed_url)
                continue

            content_type = response.headers.get("content-type", "").lower()
            text = response.text

            # Quick sanity check — is this actually XML?
            if "<rss" not in text.lower() and "<feed" not in text.lower() and "<item" not in text.lower():
                logger.debug("[RSS] Response doesn't look like RSS/Atom from %s", feed_url)
                continue

            # Try RSS 2.0 first, then Atom
            items = _parse_rss_xml(text)
            if not items:
                items = _parse_atom_xml(text)

            if items:
                logger.info(
                    "[RSS] ✅ Successfully fetched %d projects from %s",
                    len(items), feed_url,
                )
                return items[:max_items]

        except requests.RequestException as exc:
            logger.debug("[RSS] Network error for %s: %s", feed_url, exc)
            continue
        except Exception as exc:
            logger.debug("[RSS] Unexpected error for %s: %s", feed_url, exc)
            continue

    logger.warning("[RSS] ⚠️ All Mostaql RSS feeds failed. Falling back to Playwright.")
    return []


# ---------------------------------------------------------------------------
# Public API: Generic RSS Fetcher
# ---------------------------------------------------------------------------
def fetch_rss(
    feed_url: str,
    max_items: int = 30,
) -> list[dict[str, str]]:
    """
    Fetch any RSS/Atom feed and return structured items.
    Can be used for any platform that exposes a feed.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; HireWire/1.0; Feed Reader)",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
    }

    try:
        response = requests.get(feed_url, headers=headers, timeout=15)
        if response.status_code != 200:
            logger.warning("[RSS] HTTP %d from %s", response.status_code, feed_url)
            return []

        items = _parse_rss_xml(response.text)
        if not items:
            items = _parse_atom_xml(response.text)

        logger.info("[RSS] Fetched %d items from %s", len(items), feed_url)
        return items[:max_items]

    except Exception as exc:
        logger.error("[RSS] Failed to fetch %s: %s", feed_url, exc)
        return []
