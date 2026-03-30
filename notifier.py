"""
HireWire — Telegram Notification Engine
Delivers AI reports and system alerts to Telegram with HTML sanitization.
"""

import re
import time
import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, logger


# Telegram message size limit
MAX_MESSAGE_LENGTH = 4096

# Tags that Telegram HTML mode supports (case-insensitive)
TELEGRAM_ALLOWED_TAGS = {
    "b", "strong",          # bold
    "i", "em",              # italic
    "u", "ins",             # underline
    "s", "strike", "del",   # strikethrough
    "code", "pre",          # monospace
    "a",                    # links (with href)
    "blockquote",           # block quote
    "tg-spoiler",           # spoiler
}


# ---------------------------------------------------------------------------
# HTML Sanitizer — strips everything Telegram can't parse
# ---------------------------------------------------------------------------
def _sanitize_html(text: str) -> str:
    """
    Clean AI-generated HTML so it only contains tags Telegram supports.

    Telegram HTML mode supports ONLY:
    <b>, <strong>, <i>, <em>, <u>, <ins>, <s>, <strike>, <del>,
    <code>, <pre>, <a href="...">, <blockquote>, <tg-spoiler>

    Everything else (h1-h6, p, div, span, ul, ol, li, table, tr, td,
    br, hr, img, etc.) must be stripped or converted.
    """
    if not text:
        return ""

    # Step 1: Convert common block tags to newlines BEFORE stripping
    # <br>, <br/>, <br /> → newline
    text = re.sub(r"<br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)

    # <hr>, <hr/> → separator line
    text = re.sub(r"<hr\s*/?\s*>", "\n━━━━━━━━━━━━━━━━\n", text, flags=re.IGNORECASE)

    # </p>, </div>, </h1>...</h6> → newline (block-end = line break)
    text = re.sub(r"</(?:p|div|h[1-6]|tr|section|article)>", "\n", text, flags=re.IGNORECASE)

    # <li> → bullet point
    text = re.sub(r"<li\s*[^>]*>", "\n• ", text, flags=re.IGNORECASE)
    text = re.sub(r"</li>", "", text, flags=re.IGNORECASE)

    # <h1>...<h6> opening → bold (convert heading to bold)
    text = re.sub(r"<h[1-6]\s*[^>]*>", "<b>", text, flags=re.IGNORECASE)
    # The closing was already converted to \n above, add closing </b> before it
    text = re.sub(r"<b>([^<]*)\n", r"<b>\1</b>\n", text)

    # Step 2: Remove all HTML tags that are NOT in the allowed set
    def _tag_filter(match: re.Match) -> str:
        full_tag = match.group(0)
        tag_name_match = re.match(r"</?(\S+?)[\s>]", full_tag + ">")
        if not tag_name_match:
            return ""
        tag_name = tag_name_match.group(1).lower().rstrip("/")

        if tag_name in TELEGRAM_ALLOWED_TAGS:
            # For <a> tags, only keep href attribute
            if tag_name == "a" and "href" in full_tag:
                href_match = re.search(r'href=["\']([^"\']+)["\']', full_tag)
                if href_match:
                    return f'<a href="{href_match.group(1)}">'
                return ""
            elif tag_name == "a":
                return full_tag  # closing </a>
            return full_tag
        return ""

    text = re.sub(r"<[^>]+>", _tag_filter, text)

    # Step 3: Fix common issues
    # Remove empty bold/italic tags
    text = re.sub(r"<(b|strong|i|em|u)>\s*</\1>", "", text)

    # Collapse 3+ consecutive newlines into 2
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Remove leading/trailing whitespace on each line
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)

    # Step 4: Validate tag pairing — close any unclosed tags
    for tag in ["b", "strong", "i", "em", "u", "s", "code", "a"]:
        open_count = len(re.findall(f"<{tag}[\\s>]", text, re.IGNORECASE))
        close_count = len(re.findall(f"</{tag}>", text, re.IGNORECASE))
        if open_count > close_count:
            text += f"</{tag}>" * (open_count - close_count)

    return text.strip()


# ---------------------------------------------------------------------------
# Internal Sender
# ---------------------------------------------------------------------------
def _send_message(
    text: str,
    parse_mode: str | None = "HTML",
    disable_preview: bool = True,
    retries: int = 2,
) -> bool:
    """
    Send a single message to Telegram with retry logic.
    Sanitizes HTML before sending to prevent parse errors.
    Falls back to plain text only as a last resort.
    """
    if not text or not text.strip():
        return False

    # Sanitize HTML before sending
    if parse_mode == "HTML":
        text = _sanitize_html(text)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": disable_preview,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    for attempt in range(1, retries + 1):
        try:
            response = requests.post(url, json=payload, timeout=30)

            if response.status_code == 200:
                return True

            # If HTML parsing STILL failed after sanitization, strip ALL tags
            if response.status_code == 400 and parse_mode == "HTML":
                logger.warning(
                    "[Telegram] HTML still rejected after sanitization. Stripping all tags..."
                )
                clean_text = re.sub(r"<[^>]+>", "", text)
                payload["text"] = clean_text
                payload.pop("parse_mode", None)
                fallback = requests.post(url, json=payload, timeout=30)
                if fallback.status_code == 200:
                    return True
                logger.error("[Telegram] Plain text fallback also failed: %s", fallback.text)
                return False

            logger.warning(
                "[Telegram] Send attempt %d/%d failed (HTTP %d): %s",
                attempt, retries, response.status_code, response.text[:200],
            )

        except requests.exceptions.RequestException as exc:
            logger.warning(
                "[Telegram] Network error (attempt %d/%d): %s", attempt, retries, exc
            )

        if attempt < retries:
            time.sleep(5)

    logger.error("[Telegram] All %d send attempts failed.", retries)
    return False


# ---------------------------------------------------------------------------
# Message Splitter — tag-aware splitting
# ---------------------------------------------------------------------------
def _split_message(text: str) -> list[str]:
    """
    Split a long message into chunks ≤ MAX_MESSAGE_LENGTH characters.
    Tries to split at double-newlines (between projects) for clean breaks.
    """
    if len(text) <= MAX_MESSAGE_LENGTH:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= MAX_MESSAGE_LENGTH:
            chunks.append(text)
            break

        # Priority 1: Split at double-newline (between project entries)
        split_point = text.rfind("\n\n", 0, MAX_MESSAGE_LENGTH)

        # Priority 2: Split at single newline
        if split_point == -1 or split_point < MAX_MESSAGE_LENGTH // 3:
            split_point = text.rfind("\n", 0, MAX_MESSAGE_LENGTH)

        # Priority 3: Hard split at limit
        if split_point == -1 or split_point < MAX_MESSAGE_LENGTH // 3:
            split_point = MAX_MESSAGE_LENGTH

        chunks.append(text[:split_point])
        text = text[split_point:].lstrip("\n")

    return chunks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def send_report(html_text: str) -> bool:
    """
    Send the AI-generated HTML report to Telegram.
    Sanitizes HTML and handles message splitting if report exceeds 4096 chars.
    """
    if not html_text:
        return False

    # Sanitize the entire report first, then split
    sanitized = _sanitize_html(html_text)

    chunks = _split_message(sanitized)
    logger.info(
        "[Telegram] Sending report (%d chars, %d message(s))...",
        len(sanitized), len(chunks),
    )

    success = True
    for i, chunk in enumerate(chunks, 1):
        if not _send_message(chunk):
            logger.error("[Telegram] Failed to send chunk %d/%d", i, len(chunks))
            success = False
        else:
            logger.info("[Telegram] ✅ Chunk %d/%d sent", i, len(chunks))
            if i < len(chunks):
                time.sleep(1)  # Rate limiting between chunks

    return success


def send_alert(message: str) -> bool:
    """Send a plain-text system alert (errors, status updates)."""
    logger.info("[Telegram] Sending alert: %s", message[:100])
    return _send_message(message, parse_mode=None)


def send_startup_ping() -> bool:
    """Send a startup confirmation message to verify connectivity."""
    return _send_message(
        "🟢 <b>Mostaql AI Agent Started</b>\n"
        "<i>Monitoring: Mostaql + Nafezly + PeoplePerHour</i>",
        parse_mode="HTML",
    )
