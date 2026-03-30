"""
HireWire — Multi-Platform Playwright Scraper

Stage 1: Scrape the listing page → extract project titles + URLs
Stage 2: Visit EACH project URL → extract client hiring rate, budget, description
Filter:  Discard clients with hiring rate below MIN_HIRING_RATE
"""

import time
import random
import re
from playwright.sync_api import sync_playwright, Page, Browser

from config import (
    logger,
    SCRAPER_TIMEOUT_MS,
    SCRAPER_MIN_DELAY,
    SCRAPER_MAX_DELAY,
    MAX_PROJECTS_PER_RUN,
    MIN_HIRING_RATE,
)
from models import Project, ClientInfo, ScrapingResult


# ---------------------------------------------------------------------------
# Human-like delay
# ---------------------------------------------------------------------------
def _human_delay(min_s: float = SCRAPER_MIN_DELAY, max_s: float = SCRAPER_MAX_DELAY) -> None:
    """Random sleep to mimic human browsing speed."""
    time.sleep(random.uniform(min_s, max_s))


# ---------------------------------------------------------------------------
# Stage 1: List Scraper — Extract project links from listing page
# ---------------------------------------------------------------------------
def _scrape_listing_page(page: Page, url: str) -> list[dict]:
    """
    Navigate to the Mostaql listing page and extract project titles + URLs.
    Returns a list of dicts: [{"title": ..., "url": ...}, ...]
    """
    projects_raw: list[dict] = []

    logger.info("[Scraper] Stage 1 — Navigating to listing: %s", url)
    page.goto(url, timeout=SCRAPER_TIMEOUT_MS, wait_until="domcontentloaded")
    _human_delay()

    # Wait for project cards to appear
    try:
        page.wait_for_selector("h2 a, .project-title a, .project--title a", timeout=15000)
    except Exception:
        logger.warning("[Scraper] Could not find project title selectors, trying broader search...")

    # Try multiple selector strategies for project links
    selectors = [
        "h2 a[href*='/project/']",
        ".project-title a[href*='/project/']",
        ".project--title a[href*='/project/']",
        "a[href*='mostaql.com/project/']",
        "a[href*='/project/']",
    ]

    links_found: list[dict] = []
    for selector in selectors:
        elements = page.locator(selector).all()
        if elements:
            logger.info("[Scraper] Found %d project links using selector: %s", len(elements), selector)
            for elem in elements:
                try:
                    title = elem.inner_text().strip()
                    href = elem.get_attribute("href") or ""
                    if not href:
                        continue
                    full_url = href if href.startswith("http") else f"https://mostaql.com{href}"
                    if title and "/project/" in full_url:
                        links_found.append({
                            "title": title.split("\n")[0].strip(),
                            "url": full_url,
                        })
                except Exception:
                    continue
            break  # Use first selector that works

    # Deduplicate by URL
    seen_urls: set[str] = set()
    for item in links_found:
        if item["url"] not in seen_urls:
            seen_urls.add(item["url"])
            projects_raw.append(item)

    logger.info("[Scraper] Stage 1 complete — %d unique projects found on listing page", len(projects_raw))
    return projects_raw[:MAX_PROJECTS_PER_RUN]


# ---------------------------------------------------------------------------
# Stage 2: Deep Scraper — Visit each project page for client details
# ---------------------------------------------------------------------------
def _extract_float(text: str) -> float:
    """Extract the first number (int or decimal) from a string like '63.64%'."""
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else 0.0


def _scrape_project_page(page: Page, url: str, title: str) -> Project:
    """
    Visit a single project page and extract:
    - Full project description
    - Budget
    - Client name, hiring rate, total projects
    - Skills/tags
    - Number of proposals

    The client section ("صاحب المشروع") on Mostaql looks like:
        صاحب المشروع
        محمد ر.
        تاريخ التسجيل  11 أكتوبر 2023
        معدل التوظيف    63.64%          <-- THIS is what we need
        المشاريع المفتوحة  1
    """
    logger.debug("[Scraper] Stage 2 — Visiting project: %s", url)

    try:
        page.goto(url, timeout=SCRAPER_TIMEOUT_MS, wait_until="domcontentloaded")
        _human_delay(1.5, 3.5)
    except Exception as exc:
        logger.warning("[Scraper] Failed to load project page %s: %s", url, exc)
        return Project(title=title, url=url)

    client = ClientInfo()

    # =========================================================================
    # STRATEGY 1: Locate "معدل التوظيف" label, then read parent row for value
    # =========================================================================
    try:
        # Find the element that contains the label text "معدل التوظيف"
        label_locator = page.locator("text=معدل التوظيف")
        if label_locator.count() > 0:
            # The label and value are in the same parent row/container.
            # Get the text of the PARENT element which should contain both
            # "معدل التوظيف" and "63.64%"
            label_elem = label_locator.first

            # Try multiple levels of parent traversal to find the value
            for parent_selector in ["..", "../..", "../../.."]:
                try:
                    parent = label_elem.locator(parent_selector)
                    parent_text = parent.inner_text().strip()
                    # Look for a percentage number in the parent text
                    pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", parent_text)
                    if pct_match:
                        rate = float(pct_match.group(1))
                        if 0 < rate <= 100:
                            client.hiring_rate = int(rate)  # Store as int (63)
                            logger.info("  → Hiring rate found (parent traversal): %.2f%% → %d%%", rate, client.hiring_rate)
                            break
                except Exception:
                    continue

            # Alternative: check next sibling via evaluate
            if client.hiring_rate == 0:
                try:
                    # Use JS to get the next sibling or adjacent cell value
                    rate_text = label_elem.evaluate("""el => {
                        // Try: next sibling element
                        if (el.nextElementSibling) return el.nextElementSibling.innerText;
                        // Try: parent's next sibling
                        if (el.parentElement && el.parentElement.nextElementSibling)
                            return el.parentElement.nextElementSibling.innerText;
                        // Try: parent row, get second cell/column
                        const row = el.closest('tr, li, div, .row');
                        if (row) {
                            const cells = row.querySelectorAll('td, span, div');
                            for (const cell of cells) {
                                if (cell.innerText.includes('%') && cell !== el)
                                    return cell.innerText;
                            }
                        }
                        return '';
                    }""")
                    if rate_text and "%" in rate_text:
                        pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", rate_text)
                        if pct_match:
                            rate = float(pct_match.group(1))
                            if 0 < rate <= 100:
                                client.hiring_rate = int(rate)
                                logger.info("  → Hiring rate found (JS sibling): %.2f%% → %d%%", rate, client.hiring_rate)
                except Exception as js_err:
                    logger.debug("  → JS sibling extraction failed: %s", js_err)

    except Exception as e:
        logger.debug("  → Strategy 1 (label locator) failed: %s", e)

    # =========================================================================
    # STRATEGY 2: Broad page text scan with flexible regex
    # The label and value may be separated by newlines, spaces, or other text
    # =========================================================================
    if client.hiring_rate == 0:
        try:
            page_text = page.inner_text("body")
            # Allow newlines, whitespace, and other characters between label and value
            # Also handle decimal percentages like 63.64%
            patterns = [
                # "معدل التوظيف" then within 0-50 chars: a decimal/int percentage
                r"معدل\s*(?:ال)?توظيف[\s\S]{0,50}?(\d+(?:\.\d+)?)\s*%",
                r"نسبة\s*(?:ال)?توظيف[\s\S]{0,50}?(\d+(?:\.\d+)?)\s*%",
                # Reverse: percentage then label
                r"(\d+(?:\.\d+)?)\s*%[\s\S]{0,30}?(?:معدل|نسبة)\s*(?:ال)?توظيف",
                # English fallbacks
                r"hiring\s*(?:rate)?[\s\S]{0,30}?(\d+(?:\.\d+)?)\s*%",
            ]
            for pattern in patterns:
                match = re.search(pattern, page_text, re.IGNORECASE)
                if match:
                    rate = float(match.group(1))
                    if 0 < rate <= 100:
                        client.hiring_rate = int(rate)
                        logger.info("  → Hiring rate found (text regex): %.2f%% → %d%%", rate, client.hiring_rate)
                        break
        except Exception as e:
            logger.debug("  → Strategy 2 (text regex) failed: %s", e)

    # =========================================================================
    # STRATEGY 3: Look for any percentage elements in the owner/client section
    # =========================================================================
    if client.hiring_rate == 0:
        try:
            # Find the section titled "صاحب المشروع" and extract all percentages
            owner_section = page.locator("text=صاحب المشروع")
            if owner_section.count() > 0:
                # Go up to a reasonable container and get its full text
                for depth in ["../..", "../../..", "../../../.."]:
                    try:
                        container = owner_section.first.locator(depth)
                        container_text = container.inner_text().strip()
                        if "%" in container_text and "توظيف" in container_text:
                            pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", container_text)
                            if pct_match:
                                rate = float(pct_match.group(1))
                                if 0 < rate <= 100:
                                    client.hiring_rate = int(rate)
                                    logger.info("  → Hiring rate found (owner section): %.2f%% → %d%%", rate, client.hiring_rate)
                                    break
                    except Exception:
                        continue
        except Exception as e:
            logger.debug("  → Strategy 3 (owner section) failed: %s", e)

    if client.hiring_rate == 0:
        logger.debug("  → ⚠️ Could not extract hiring rate for: %s", url)

    # --- Extract Client Name ---
    # From the screenshot: the name appears under "صاحب المشروع" header
    client_name_selectors = [
        ".owner-name", ".client-name", ".user-name",
        ".project-owner a", ".owner-card a",
    ]
    for selector in client_name_selectors:
        try:
            elem = page.locator(selector).first
            if elem.count() > 0:
                client.name = elem.inner_text().strip()
                break
        except Exception:
            continue

    # Fallback: extract name from the owner section text
    if not client.name:
        try:
            owner_label = page.locator("text=صاحب المشروع")
            if owner_label.count() > 0:
                parent_text = owner_label.first.locator("../..").inner_text().strip()
                lines = [l.strip() for l in parent_text.split("\n") if l.strip()]
                # The name is typically the line right after "صاحب المشروع"
                for idx, line in enumerate(lines):
                    if "صاحب المشروع" in line and idx + 1 < len(lines):
                        client.name = lines[idx + 1]
                        break
        except Exception:
            pass

    # --- Extract Project Description ---
    description = ""
    desc_selectors = [
        ".project-description", ".project--description",
        ".project-content", ".ckeditor-content",
        "article", ".details-text",
    ]
    for selector in desc_selectors:
        try:
            elem = page.locator(selector).first
            if elem.count() > 0:
                description = elem.inner_text().strip()[:1000]
                break
        except Exception:
            continue

    # Fallback: look for "تفاصيل المشروع" section
    if not description:
        try:
            details_label = page.locator("text=تفاصيل المشروع")
            if details_label.count() > 0:
                parent = details_label.first.locator("..")
                description = parent.inner_text().strip()[:1000]
        except Exception:
            pass

    # --- Extract Budget ---
    budget = ""
    budget_selectors = [
        ".project-budget", ".budget", "[class*='budget']",
        ".project-meta .price",
    ]
    for selector in budget_selectors:
        try:
            elem = page.locator(selector).first
            if elem.count() > 0:
                budget = elem.inner_text().strip()
                break
        except Exception:
            continue

    # Fallback: look for "الميزانية" label
    if not budget:
        try:
            budget_label = page.locator("text=الميزانية")
            if budget_label.count() > 0:
                parent_text = budget_label.first.locator("..").inner_text().strip()
                # Extract line with $ or numbers
                for line in parent_text.split("\n"):
                    if "$" in line or re.search(r"\d+\.\d+", line):
                        budget = line.strip()
                        break
        except Exception:
            pass

    # --- Extract Skills/Tags ---
    skills: list[str] = []
    skill_selectors = [
        ".skills a", ".tags a", ".project-skills a",
        "[class*='skill'] a", "[class*='tag'] a",
    ]
    for selector in skill_selectors:
        try:
            elems = page.locator(selector).all()
            skills = [e.inner_text().strip() for e in elems if e.inner_text().strip()]
            if skills:
                break
        except Exception:
            continue

    # Fallback: look for "المهارات" section
    if not skills:
        try:
            skills_label = page.locator("text=المهارات")
            if skills_label.count() > 0:
                parent_text = skills_label.first.locator("../..").inner_text().strip()
                # Extract tag-like items (short text fragments)
                for line in parent_text.split("\n"):
                    line = line.strip()
                    if line and line != "المهارات" and len(line) < 50:
                        skills.append(line)
        except Exception:
            pass

    # --- Extract Proposals Count ---
    proposals = ""
    try:
        proposals_label = page.locator("text=عرض")
        if proposals_label.count() > 0:
            for elem in proposals_label.all():
                text = elem.inner_text().strip()
                match = re.search(r"(\d+)\s*عرض", text)
                if match:
                    proposals = match.group(1)
                    break
    except Exception:
        pass

    project = Project(
        title=title,
        url=url,
        description=description,
        budget=budget,
        skills=skills,
        proposals_count=proposals,
        client=client,
    )

    logger.info(
        "    ↳ 👤 Owner: %-15s | 💼 Hiring: %3d%% | 💰 Budget: %s",
        (client.name or "Unknown")[:15], client.hiring_rate, budget or "N/A",
    )

    return project


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------
def scrape_mostaql(url: str) -> ScrapingResult:
    """
    Full two-stage scraping pipeline:
    1. Scrape listing page for project URLs
    2. Visit each project page to extract client hiring rate
    3. Filter out unserious clients (hiring rate < MIN_HIRING_RATE)
    """
    result = ScrapingResult()

    with sync_playwright() as pw:
        browser: Browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="ar-SA",
        )
        page = context.new_page()

        try:
            # Stage 1: Get project links
            raw_projects = _scrape_listing_page(page, url)
            result.total_on_page = len(raw_projects)

            if not raw_projects:
                logger.warning("[Scraper] No projects found on listing page.")
                return result

            # Stage 2: Deep-scrape each project page
            serious_projects: list[Project] = []

            for i, raw in enumerate(raw_projects, 1):
                logger.info(
                    "[Scraper] Deep-scraping project %d/%d: %s",
                    i, len(raw_projects), raw["title"][:50],
                )

                project = _scrape_project_page(page, raw["url"], raw["title"])

                if project.client.hiring_rate >= MIN_HIRING_RATE:
                    serious_projects.append(project)
                    result.serious_clients += 1
                    logger.info("    ↳ ✅ Kept: Hiring rate >= %d%%", MIN_HIRING_RATE)
                else:
                    result.filtered_out += 1
                    logger.info("    ↳ ❌ Filtered: Rate %d%% < %d%%", project.client.hiring_rate, MIN_HIRING_RATE)

                # Human-like delay between project pages
                if i < len(raw_projects):
                    _human_delay(1.0, 2.5)

            result.projects = serious_projects
            result.new_found = len(serious_projects)

        except Exception as exc:
            logger.error("[Scraper] Critical error during scraping: %s", exc, exc_info=True)
            # Save screenshot for debugging
            try:
                page.screenshot(path="logs/scraper_error.png")
                logger.info("[Scraper] Error screenshot saved to logs/scraper_error.png")
            except Exception:
                pass
        finally:
            browser.close()

    logger.info(result.summary())
    return result


# ---------------------------------------------------------------------------
# Nafezly — Listing page scraper
# ---------------------------------------------------------------------------
def _scrape_nafezly_listing(page: Page, url: str) -> list[dict]:
    """
    Scrape Nafezly listing page for project titles + URLs.
    Based on the page structure: each project card has a title link
    pointing to /projects/{slug}.
    """
    projects_raw: list[dict] = []

    logger.info("[Nafezly] Stage 1 — Navigating to listing: %s", url)
    page.goto(url, timeout=SCRAPER_TIMEOUT_MS, wait_until="domcontentloaded")
    _human_delay()

    # Wait for project cards — Nafezly uses div.project-box
    try:
        page.wait_for_selector("div.project-box", timeout=15000)
    except Exception:
        logger.warning("[Nafezly] Could not find project links on listing page.")

    # Extract project links — Nafezly uses a.text-truncate inside div.project-box
    # URLs use /project/{id}-{slug} (singular, not /projects/)
    selectors = [
        "div.project-box a.text-truncate",
        "div.project-box h3 a",
        "a.text-truncate[href*='/project/']",
        "a[href*='nafezly.com/project/']",
    ]

    links_found: list[dict] = []
    for selector in selectors:
        elements = page.locator(selector).all()
        if elements:
            logger.info("[Nafezly] Found %d links via: %s", len(elements), selector)
            for elem in elements:
                try:
                    title = elem.inner_text().strip()
                    href = elem.get_attribute("href") or ""
                    if not href:
                        continue
                    # Skip category/filter links
                    if "specialize=" in href or "page=" in href:
                        continue
                    full_url = href if href.startswith("http") else f"https://nafezly.com{href}"
                    if title and "/project/" in full_url:
                        links_found.append({
                            "title": title.split("\n")[0].strip(),
                            "url": full_url,
                        })
                except Exception:
                    continue
            break

    # Deduplicate
    seen: set[str] = set()
    for item in links_found:
        if item["url"] not in seen:
            seen.add(item["url"])
            projects_raw.append(item)

    logger.info("[Nafezly] Stage 1 complete — %d unique projects found", len(projects_raw))
    return projects_raw[:MAX_PROJECTS_PER_RUN]


# ---------------------------------------------------------------------------
# Nafezly — Project page scraper
# ---------------------------------------------------------------------------
def _scrape_nafezly_project(page: Page, url: str, title: str) -> Project:
    """
    Visit a Nafezly project page and extract details.
    Nafezly layout (from screenshots):
      - بطاقة المشروع: status, date, duration, budget, applicants
      - تفاصيل المشروع: description
      - صاحب المشروع: client name
      - مهارات مطلوبة: skills/tags

    NOTE: Nafezly does NOT show hiring rate, so we set it to -1
    to indicate "not available" (will bypass the hiring filter).
    """
    logger.debug("[Nafezly] Visiting project: %s", url)

    try:
        page.goto(url, timeout=SCRAPER_TIMEOUT_MS, wait_until="domcontentloaded")
        _human_delay(1.5, 3.5)
    except Exception as exc:
        logger.warning("[Nafezly] Failed to load: %s — %s", url, exc)
        return Project(title=title, url=url, source="nafezly")

    client = ClientInfo()
    # Mark hiring rate as -1 = "not available on this platform"
    client.hiring_rate = -1

    # --- Client Name ---
    try:
        owner_label = page.locator("text=صاحب المشروع")
        if owner_label.count() > 0:
            parent_text = owner_label.first.locator("../..").inner_text().strip()
            lines = [l.strip() for l in parent_text.split("\n") if l.strip()]
            for idx, line in enumerate(lines):
                if "صاحب المشروع" in line and idx + 1 < len(lines):
                    client.name = lines[idx + 1]
                    break
    except Exception:
        pass

    # --- Description ---
    description = ""
    try:
        details_label = page.locator("text=تفاصيل المشروع")
        if details_label.count() > 0:
            parent = details_label.first.locator("..")
            description = parent.inner_text().strip()[:1000]
            # Remove the label text itself
            description = description.replace("تفاصيل المشروع", "").strip()
    except Exception:
        pass

    # --- Budget ---
    budget = ""
    try:
        budget_label = page.locator("text=الميزانية")
        if budget_label.count() > 0:
            parent_text = budget_label.first.locator("..").inner_text().strip()
            for line in parent_text.split("\n"):
                if "$" in line or re.search(r"\d+\s*-\s*\d+", line):
                    budget = line.strip()
                    break
    except Exception:
        pass

    # --- Skills ---
    skills: list[str] = []
    try:
        skills_label = page.locator("text=مهارات مطلوبة")
        if skills_label.count() > 0:
            parent_text = skills_label.first.locator("../..").inner_text().strip()
            for line in parent_text.split("\n"):
                line = line.strip()
                if line and line != "مهارات مطلوبة" and len(line) < 50:
                    skills.append(line)
    except Exception:
        pass

    # --- Proposals count ---
    proposals = ""
    try:
        page_text = page.inner_text("body")
        match = re.search(r"(\d+)\s*(?:عروض|عرض)", page_text)
        if match:
            proposals = match.group(1)
    except Exception:
        pass

    project = Project(
        title=title,
        url=url,
        description=description,
        budget=budget,
        skills=skills,
        proposals_count=proposals,
        client=client,
        source="nafezly",
    )

    logger.info(
        "    ↳ 👤 Owner: %-15s | 💰 Budget: %s | 🏷️ Nafezly",
        (client.name or "Unknown")[:15], budget or "N/A",
    )

    return project


# ---------------------------------------------------------------------------
# Nafezly — Main Entry Point
# ---------------------------------------------------------------------------
def scrape_nafezly(url: str) -> ScrapingResult:
    """
    Full scraping pipeline for Nafezly.
    Since Nafezly doesn't show hiring rate, all projects are included.
    """
    result = ScrapingResult()

    with sync_playwright() as pw:
        browser: Browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="ar-SA",
        )
        page = context.new_page()

        try:
            raw_projects = _scrape_nafezly_listing(page, url)
            result.total_on_page = len(raw_projects)

            if not raw_projects:
                logger.warning("[Nafezly] No projects found on listing page.")
                return result

            all_projects: list[Project] = []

            for i, raw in enumerate(raw_projects, 1):
                logger.info(
                    "[Nafezly] Deep-scraping project %d/%d: %s",
                    i, len(raw_projects), raw["title"][:50],
                )

                project = _scrape_nafezly_project(page, raw["url"], raw["title"])
                all_projects.append(project)
                result.serious_clients += 1

                if i < len(raw_projects):
                    _human_delay(1.0, 2.5)

            result.projects = all_projects
            result.new_found = len(all_projects)

        except Exception as exc:
            logger.error("[Nafezly] Critical error: %s", exc, exc_info=True)
            try:
                page.screenshot(path="logs/nafezly_error.png")
            except Exception:
                pass
        finally:
            browser.close()

    logger.info(result.summary())
    return result


# ===========================================================================
# PeoplePerHour (PPH) Scraper
# ===========================================================================


# ---------------------------------------------------------------------------
# PPH — Listing page scraper
# ---------------------------------------------------------------------------
def _scrape_pph_listing(page: Page, url: str) -> list[dict]:
    """
    Scrape PeoplePerHour listing page for job titles + URLs.
    PPH uses CSS-module dynamic classes, so we use partial-match selectors:
      - Card container: div[class*="ListItem__container"]
      - Title link:     a[class*="item__url"]
    URL pattern: /freelance-jobs/{category}/{subcategory}/{title-slug}-{id}
    """
    projects_raw: list[dict] = []

    logger.info("[PPH] Stage 1 — Navigating to listing: %s", url)
    page.goto(url, timeout=SCRAPER_TIMEOUT_MS, wait_until="domcontentloaded")
    _human_delay(2.0, 4.0)

    # Wait for job cards to render (React app, may take a moment)
    try:
        page.wait_for_selector("a[class*='item__url'], a[href*='/freelance-jobs/']", timeout=20000)
    except Exception:
        logger.warning("[PPH] Could not find job cards on listing page.")

    # Extract job links with fallback selectors
    selectors = [
        "a[class*='item__url']",
        "div[class*='ListItem'] a[href*='/freelance-jobs/']",
        "a[href*='/freelance-jobs/'][href*='-']",
    ]

    links_found: list[dict] = []
    for selector in selectors:
        elements = page.locator(selector).all()
        if elements:
            logger.info("[PPH] Found %d links via: %s", len(elements), selector)
            for elem in elements:
                try:
                    title = elem.inner_text().strip()
                    href = elem.get_attribute("href") or ""
                    if not href or not title:
                        continue
                    # Must be a job detail page (has category + slug + numeric ID)
                    full_url = href if href.startswith("http") else f"https://www.peopleperhour.com{href}"
                    # Skip if it's just the /freelance-jobs listing page itself
                    if full_url.rstrip("/") == "https://www.peopleperhour.com/freelance-jobs":
                        continue
                    # Must have at least 3 path segments to be a detail page
                    path_parts = full_url.replace("https://www.peopleperhour.com/freelance-jobs/", "").split("/")
                    if len(path_parts) >= 2:
                        # Extract "by [Name]" from the listing card (parent container)
                        listing_client_name = ""
                        try:
                            card = elem.locator("xpath=ancestor::div[contains(@class,'ListItem') or contains(@class,'card')]")
                            if card.count() > 0:
                                card_text = card.first.inner_text()
                                by_match = re.search(r"\bby\s+([A-Z][A-Za-z]+(?:\s+[A-Z]\.)?)", card_text)
                                if by_match:
                                    listing_client_name = by_match.group(1).strip()
                        except Exception:
                            pass
                        links_found.append({
                            "title": title.split("\n")[0].strip(),
                            "url": full_url,
                            "client_name": listing_client_name,
                        })
                except Exception:
                    continue
            break

    # Deduplicate
    seen: set[str] = set()
    for item in links_found:
        if item["url"] not in seen:
            seen.add(item["url"])
            projects_raw.append(item)

    logger.info("[PPH] Stage 1 complete — %d unique jobs found", len(projects_raw))
    return projects_raw[:MAX_PROJECTS_PER_RUN]


# ---------------------------------------------------------------------------
# PPH — Project page scraper
# ---------------------------------------------------------------------------
def _scrape_pph_project(page: Page, url: str, title: str, listing_client_name: str = "") -> Project:
    """
    Visit a PeoplePerHour job detail page and extract structured data.

    PPH page layout (verified from screenshots):
    ┌──────────────────────────────────────────────────────────┐
    │  Title                                                   │
    │  SEND PROPOSAL                                           │
    │  ENDS IN (DAYS) 29       │  FIXED PRICE $25              │
    │                          │  PER HOUR $41/hr              │
    │                          │  (APPROX. $60/hr)             │
    │  Posted: 3 min ago · Proposals: 5 · Remote #4484297      │
    │  OPEN FOR PROPOSALS                                      │
    ├──────────────────────────────────────────────────────────┤
    │  Description                                             │
    │  Experience Level: Expert                                │
    │  ...text...                                              │
    ├──────────────────────────────────────────────────────────┤
    │  Client Name         │  PROJECTS COMPLETED              │
    │  👍 100% (16)        │  13                               │
    │                      │  FREELANCERS WORKED WITH          │
    │                      │  10                               │
    │                      │  PROJECTS AWARDED                 │
    │                      │  29%                              │
    │                      │  LAST PROJECT                     │
    │                      │  22 Apr 2025                      │
    │                      │  📍 UNITED KINGDOM                │
    └──────────────────────────────────────────────────────────┘

    CRITICAL: Budget MUST come from the structured header ("FIXED PRICE $25"
    or "PER HOUR $41/hr"), NOT from the description body which may contain
    informal/misleading budget notes.
    """
    logger.debug("[PPH] Visiting job: %s", url)

    try:
        page.goto(url, timeout=SCRAPER_TIMEOUT_MS, wait_until="domcontentloaded")
        _human_delay(2.0, 4.0)
    except Exception as exc:
        logger.warning("[PPH] Failed to load: %s — %s", url, exc)
        return Project(title=title, url=url, source="pph")

    # Get full page text ONCE for all extractions
    try:
        page_text = page.inner_text("body")
    except Exception:
        page_text = ""

    client = ClientInfo()
    client.hiring_rate = -1  # Will try to extract from sidebar

    # =========================================================================
    # BUDGET — Extract from structured header ONLY (never from description)
    # =========================================================================
    # PPH has two pricing modes visible in the header:
    #   "FIXED PRICE\n$25"               → Fixed price job
    #   "PER HOUR\n$41/hr"               → Hourly rate job
    #   "(APPROX. $60/hr)"               → Optional USD conversion
    #   "ENDS IN (DAYS)\n29"             → Days remaining
    # =========================================================================
    budget = ""
    pricing_type = ""  # "fixed" or "hourly"
    try:
        # --- Priority 1: "PER HOUR" header with rate ---
        # Pattern: "PER HOUR\n$41/hr" or "PER HOUR\n£45/hr"
        hourly_match = re.search(
            r"PER\s*HOUR\s*\n?\s*[£$€]\s*([\d,.]+)\s*/\s*hr",
            page_text, re.IGNORECASE
        )
        if hourly_match:
            rate = hourly_match.group(1)
            currency = "£" if "£" in hourly_match.group(0) else "$"
            budget = f"{currency}{rate}/hr (Hourly)"
            pricing_type = "hourly"

            # Check for APPROX USD conversion
            approx_match = re.search(
                r"\(APPROX\.\s*\$\s*([\d,.]+)\s*/\s*hr\s*\)",
                page_text, re.IGNORECASE
            )
            if approx_match and currency != "$":
                budget = f"{currency}{rate}/hr ≈ ${approx_match.group(1)}/hr (Hourly)"

        # --- Priority 2: "FIXED PRICE" header with amount ---
        # Pattern: "FIXED PRICE\n$25" or "FIXED PRICE\n£500"
        if not budget:
            fixed_match = re.search(
                r"FIXED\s*PRICE\s*\n?\s*[£$€]\s*([\d,.]+)",
                page_text, re.IGNORECASE
            )
            if fixed_match:
                amount = fixed_match.group(1)
                currency = "£" if "£" in fixed_match.group(0) else "$"
                budget = f"{currency}{amount} (Fixed Price)"
                pricing_type = "fixed"

                # Check for APPROX USD conversion
                approx_match = re.search(
                    r"\(APPROX\.\s*\$\s*([\d,.]+)\s*\)",
                    page_text, re.IGNORECASE
                )
                if approx_match and currency != "$":
                    budget = f"{currency}{amount} ≈ ${approx_match.group(1)} (Fixed Price)"

    except Exception as exc:
        logger.debug("[PPH] Budget extraction error: %s", exc)

    # =========================================================================
    # CLIENT NAME + RATING
    # =========================================================================
    # PPH detail page shows client name in the sidebar as a link with class
    # "member-short-name" (e.g., "Charles G.", "Samantha B.").
    # Below the name is the rating: "👍 100% (4)"
    try:
        name_found = False

        # --- Priority 1: Correct PPH DOM selectors (verified via inspection) ---
        name_selectors = [
            "a.member-short-name",              # Exact class
            "a[class*='member-short-name']",     # Partial match
            "a.crop.member-short-name",          # Full composite class
            "a[class*='card__user-link']",       # Listing page variant
            "a[class*='user-link']",             # Generic fallback
            "div[class*='client'] a",            # Container-based
        ]
        for sel in name_selectors:
            try:
                elems = page.locator(sel).all()
                if elems:
                    raw_name = elems[0].inner_text().strip()
                    if raw_name and len(raw_name) < 60:
                        client.name = raw_name.split("\n")[0].strip()
                        name_found = True
                        break
            except Exception:
                continue

        # --- Priority 2: Title attribute on links (often has full name) ---
        if not name_found:
            try:
                for sel in ["a.member-short-name", "a[class*='member-short']"]:
                    elems = page.locator(sel).all()
                    if elems:
                        title_attr = elems[0].get_attribute("title")
                        if title_attr and len(title_attr) < 60:
                            client.name = title_attr.strip()
                            name_found = True
                            break
            except Exception:
                pass

        # --- Priority 3: Text-based regex (name appears before 👍 XX% (N)) ---
        if not name_found:
            # Pattern: "SomeName\n👍 XX% (N)" or "SomeName\n100% (16)"
            name_match = re.search(
                r"\n([A-Za-z][A-Za-z\s.]{1,30})\s*\n\s*(?:👍\s*)?\d+%\s*\(\d+\)",
                page_text
            )
            if name_match:
                candidate = name_match.group(1).strip()
                # Filter out false positives (keywords that aren't names)
                skip_words = {"description", "proposal", "send", "open", "remote", "related"}
                if candidate.lower() not in skip_words and len(candidate) > 2:
                    client.name = candidate
                    name_found = True

        # --- Priority 4: "by [Name]" in the header meta area ---
        if not name_found:
            by_match = re.search(r"\bby\s+([A-Z][A-Za-z]+\s+[A-Z]\.)", page_text)
            if by_match:
                client.name = by_match.group(1).strip()
                name_found = True

        # --- Priority 5: Name from listing page (passed as parameter) ---
        if not name_found and listing_client_name:
            client.name = listing_client_name

    except Exception:
        pass

    # =========================================================================
    # CLIENT STATS — from sidebar stats table
    # =========================================================================
    try:
        # Projects Awarded percentage → acts as "hiring rate" quality signal
        awarded_match = re.search(
            r"PROJECTS\s*AWARDED\s*\n?\s*(\d+)\s*%",
            page_text, re.IGNORECASE
        )
        if awarded_match:
            client.hiring_rate = int(awarded_match.group(1))

        # Projects Completed
        completed_match = re.search(
            r"PROJECTS\s*COMPLETED\s*\n?\s*(\d+)",
            page_text, re.IGNORECASE
        )
        if completed_match:
            client.total_projects = int(completed_match.group(1))

        # Country / location
        country_match = re.search(
            r"📍\s*([A-Z][A-Z\s]+)",
            page_text
        )
        if country_match:
            client.country = country_match.group(1).strip()
        else:
            # Fallback: look for "LAST PROJECT\n...\n📍 COUNTRY" or just country after stats
            loc_match = re.search(
                r"LAST\s*PROJECT\s*\n?\s*[\w\s]+\n?\s*📍?\s*([A-Z][A-Za-z\s]+?)(?:\n|$)",
                page_text, re.IGNORECASE
            )
            if loc_match:
                client.country = loc_match.group(1).strip()

    except Exception:
        pass

    # =========================================================================
    # PROPOSALS COUNT — from the meta line: "Proposals: 5"
    # =========================================================================
    proposals = ""
    try:
        prop_match = re.search(r"Proposals:\s*(\d+)", page_text, re.IGNORECASE)
        if prop_match:
            proposals = prop_match.group(1)
    except Exception:
        pass

    # =========================================================================
    # EXPERIENCE LEVEL — "Experience Level: Expert" / "Entry" / "Intermediate"
    # =========================================================================
    experience_level = ""
    try:
        exp_match = re.search(
            r"Experience\s*Level:\s*(Expert|Intermediate|Entry)",
            page_text, re.IGNORECASE
        )
        if exp_match:
            experience_level = exp_match.group(1).strip()
    except Exception:
        pass

    # =========================================================================
    # DAYS REMAINING — "ENDS IN (DAYS)\n29"
    # =========================================================================
    days_remaining = ""
    try:
        days_match = re.search(
            r"ENDS\s*IN\s*\(DAYS\)\s*\n?\s*(\d+)",
            page_text, re.IGNORECASE
        )
        if days_match:
            days_remaining = days_match.group(1)
    except Exception:
        pass

    # =========================================================================
    # POSTED TIME — "Posted: 14 minutes ago"
    # =========================================================================
    time_posted = ""
    try:
        posted_match = re.search(
            r"Posted:\s*(.+?)(?:\s*·|\s*Proposals|\n)",
            page_text, re.IGNORECASE
        )
        if posted_match:
            time_posted = posted_match.group(1).strip()
    except Exception:
        pass

    # =========================================================================
    # DESCRIPTION — main job description text
    # =========================================================================
    description = ""
    try:
        # Look for description section between "Description" heading and client area
        desc_selectors = [
            "div[class*='Description']",
            "div[class*='description']",
        ]
        for sel in desc_selectors:
            elems = page.locator(sel).all()
            if elems:
                raw_desc = elems[0].inner_text().strip()
                # Remove the "Description" heading itself
                raw_desc = re.sub(r"^Description\s*\n?", "", raw_desc, flags=re.IGNORECASE)
                description = raw_desc[:1500]
                break

        # Fallback: extract from page text between "Description" and known anchors
        if not description:
            desc_match = re.search(
                r"Description\s*\n(.+?)(?:\n\s*(?:New Proposal|Clarification Board|RELATED|Send Proposal))",
                page_text, re.DOTALL | re.IGNORECASE
            )
            if desc_match:
                description = desc_match.group(1).strip()[:1500]
    except Exception:
        pass

    # =========================================================================
    # SKILLS/TAGS — "RELATED PROJECT SEARCHES" section
    # =========================================================================
    skills: list[str] = []
    try:
        tag_selectors = [
            "a[class*='tag']",
            "span[class*='tag']",
        ]
        for sel in tag_selectors:
            elems = page.locator(sel).all()
            if elems:
                for e in elems[:10]:
                    tag_text = e.inner_text().strip()
                    if tag_text and len(tag_text) < 50:
                        skills.append(tag_text)
                break

        # Fallback: extract from "RELATED PROJECT SEARCHES" section
        if not skills:
            related_match = re.search(
                r"RELATED\s*PROJECT\s*SEARCHES\s*\n(.+?)(?:\n\s*(?:Clarification|New Proposal|$))",
                page_text, re.DOTALL | re.IGNORECASE
            )
            if related_match:
                for line in related_match.group(1).split("\n"):
                    tag = line.strip()
                    if tag and len(tag) < 50 and not tag.startswith("POST"):
                        skills.append(tag)
    except Exception:
        pass

    # =========================================================================
    # Build the Project object
    # =========================================================================
    # Compose a richer description with experience level
    if experience_level and experience_level not in description:
        description = f"[{experience_level}] {description}"

    project = Project(
        title=title,
        url=url,
        description=description,
        budget=budget,
        time_posted=time_posted,
        skills=skills,
        proposals_count=proposals,
        client=client,
        source="pph",
    )

    logger.info(
        "    ↳ 💰 %-28s | 👤 %-12s | 📊 Awarded: %s%% | ⏰ %s | 📋 %s proposals | 🌐 PPH",
        budget or "N/A",
        (client.name or "?")[:12],
        client.hiring_rate if client.hiring_rate >= 0 else "N/A",
        time_posted or "?",
        proposals or "0",
    )

    return project


# ---------------------------------------------------------------------------
# PPH — Main Entry Point
# ---------------------------------------------------------------------------
def scrape_pph(url: str) -> ScrapingResult:
    """
    Full scraping pipeline for PeoplePerHour.
    Uses client 'Projects Awarded' percentage as a quality signal.
    """
    result = ScrapingResult()

    with sync_playwright() as pw:
        browser: Browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-GB",
        )
        page = context.new_page()

        try:
            raw_jobs = _scrape_pph_listing(page, url)
            result.total_on_page = len(raw_jobs)

            if not raw_jobs:
                logger.warning("[PPH] No jobs found on listing page.")
                return result

            all_projects: list[Project] = []

            for i, raw in enumerate(raw_jobs, 1):
                logger.info(
                    "[PPH] Deep-scraping job %d/%d: %s",
                    i, len(raw_jobs), raw["title"][:50],
                )

                project = _scrape_pph_project(page, raw["url"], raw["title"], raw.get("client_name", ""))
                all_projects.append(project)
                result.serious_clients += 1

                if i < len(raw_jobs):
                    _human_delay(1.5, 3.0)

            result.projects = all_projects
            result.new_found = len(all_projects)

        except Exception as exc:
            logger.error("[PPH] Critical error: %s", exc, exc_info=True)
            try:
                page.screenshot(path="logs/pph_error.png")
            except Exception:
                pass
        finally:
            browser.close()

    logger.info(result.summary())
    return result


# ===========================================================================
# Guru.com Scraper
# ===========================================================================


# ---------------------------------------------------------------------------
# Guru — Listing page scraper
# ---------------------------------------------------------------------------
def _scrape_guru_listing(page: Page, url: str) -> list[dict]:
    """
    Scrape Guru listing page for job titles + URLs.
    Uses standard class names (e.g., .jobRecord)
    """
    projects_raw: list[dict] = []

    logger.info("[Guru] Stage 1 — Navigating to listing: %s", url)
    page.goto(url, timeout=SCRAPER_TIMEOUT_MS, wait_until="domcontentloaded")
    _human_delay(2.0, 4.0)

    try:
        page.wait_for_selector(".jobRecord", timeout=20000)
    except Exception:
        logger.warning("[Guru] Could not find job cards on listing page.")

    # Extract job links via .jobRecord__title a
    elements = page.locator(".jobRecord__title a").all()
    if elements:
        logger.info("[Guru] Found %d links via .jobRecord__title a", len(elements))
        for elem in elements:
            try:
                title = elem.inner_text().strip()
                href = elem.get_attribute("href") or ""
                if not href or not title:
                    continue
                
                full_url = href if href.startswith("http") else f"https://www.guru.com{href}"
                
                # Deduplicate and add
                if all(p["url"] != full_url for p in projects_raw):
                    projects_raw.append({
                        "title": title.replace("\n", " ").strip(),
                        "url": full_url,
                    })
            except Exception:
                continue

    logger.info("[Guru] Stage 1 complete — %d unique jobs found", len(projects_raw))
    return projects_raw[:MAX_PROJECTS_PER_RUN]


# ---------------------------------------------------------------------------
# Guru — Project page scraper
# ---------------------------------------------------------------------------
def _scrape_guru_project(page: Page, url: str, title: str) -> Project:
    """
    Visit a Guru job detail page and extract structured data.
    """
    logger.debug("[Guru] Visiting job: %s", url)

    try:
        page.goto(url, timeout=SCRAPER_TIMEOUT_MS, wait_until="domcontentloaded")
        _human_delay(2.0, 4.0)
    except Exception as exc:
        logger.warning("[Guru] Failed to load: %s — %s", url, exc)
        return Project(title=title, url=url, source="guru")

    client = ClientInfo()
    client.hiring_rate = 0  # Default 0% unless we find positive stats

    # =========================================================================
    # BUDGET — .job-header__budget
    # =========================================================================
    budget = ""
    try:
        budgets = page.locator(".job-header__budget").all()
        if budgets:
            budget = budgets[0].inner_text().strip().replace("\n", " ")
    except Exception:
        pass

    # =========================================================================
    # CLIENT NAME — .client-info__name or strong tag
    # =========================================================================
    try:
        names = page.locator(".client-info__name").all()
        if names:
            client.name = names[0].inner_text().strip()
        else:
            # Fallback for sidebar
            strongs = page.locator("aside strong").all()
            for st in strongs:
                t = st.inner_text().strip()
                if t and len(t) < 50 and "ago" not in t.lower():
                    client.name = t
                    break
    except Exception:
        pass

    # =========================================================================
    # CLIENT STATS — Feedback % and Total Spend
    # =========================================================================
    try:
        stats_text = ""
        stats_elems = page.locator(".client-info__stats").all()
        if stats_elems:
            stats_text = stats_elems[0].inner_text().strip()
        else:
            # Fallback to general sidebar text
            asides = page.locator("aside").all()
            if asides:
                stats_text = asides[0].inner_text().strip()

        # Extract Total Spend
        spend_match = re.search(r"Spend:\s*\$?([\d,.]+)K?", stats_text, re.IGNORECASE)
        has_spent = False
        if spend_match:
            try:
                spend_val = float(spend_match.group(1).replace(",", ""))
                if "K" in spend_match.group(0).upper():
                    spend_val *= 1000
                if spend_val > 0:
                    has_spent = True
            except ValueError:
                pass

        # Extract Feedback %
        feedback_match = re.search(r"(\d+)%\s*Feedback", stats_text, re.IGNORECASE)
        if feedback_match:
            client.hiring_rate = int(feedback_match.group(1))
        elif has_spent:
            # If they spent money but lack a feedback %, user considers them serious
            client.hiring_rate = 100

        # Extract Jobs Posted
        jobs_match = re.search(r"(\d+)\s*Jobs", stats_text, re.IGNORECASE)
        if jobs_match:
            client.total_projects = int(jobs_match.group(1))

        # Extract location flag/country (usually listed nearby)
        loc_elems = page.locator(".client-info__location").all()
        if loc_elems:
            client.country = loc_elems[0].inner_text().strip()

    except Exception:
        pass

    # =========================================================================
    # TIME POSTED & PROPOSALS
    # =========================================================================
    time_posted = ""
    proposals = ""
    try:
        meta_elems = page.locator(".job-header__meta").all()
        if meta_elems:
            meta_text = meta_elems[0].inner_text().strip()
            
            # Time e.g., "Posted 3 hours ago"
            time_match = re.search(r"Posted\s+([^·\n]+)", meta_text, re.IGNORECASE)
            if time_match:
                time_posted = time_match.group(1).strip()
            
            # Proposals e.g., "Quotes (31)" or "31 Quotes"
            quotes_match = re.search(r"(\d+)\s*Quotes?|Quotes?\s*\((\d+)\)", meta_text, re.IGNORECASE)
            if quotes_match:
                proposals = quotes_match.group(1) or quotes_match.group(2)
    except Exception:
        pass

    # =========================================================================
    # DESCRIPTION
    # =========================================================================
    description = ""
    try:
        desc_elems = page.locator(".job-description").all()
        if desc_elems:
            description = desc_elems[0].inner_text().strip()[:1500]
    except Exception:
        pass

    # =========================================================================
    # SKILLS
    # =========================================================================
    skills: list[str] = []
    try:
        skill_elems = page.locator(".skillsList__skill").all()
        for e in skill_elems[:10]:
            tag = e.inner_text().strip()
            if tag:
                skills.append(tag)
    except Exception:
        pass

    project = Project(
        title=title,
        url=url,
        description=description,
        budget=budget,
        time_posted=time_posted,
        skills=skills,
        proposals_count=proposals,
        client=client,
        source="guru",
    )

    logger.info(
        "    ↳ 💰 %-28s | 👤 %-12s | 📊 Feedback: %s%% | ⏰ %s | 📋 %s proposals | 🌐 Guru",
        budget or "N/A",
        (client.name or "?")[:12],
        client.hiring_rate if client.hiring_rate > 0 else "N/A",
        time_posted or "?",
        proposals or "0",
    )

    return project


# ---------------------------------------------------------------------------
# Guru — Main Entry Point
# ---------------------------------------------------------------------------
def scrape_guru(url: str) -> ScrapingResult:
    """
    Full scraping pipeline for Guru.com.
    """
    result = ScrapingResult()

    with sync_playwright() as pw:
        browser: Browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        page = context.new_page()

        try:
            raw_jobs = _scrape_guru_listing(page, url)
            result.total_on_page = len(raw_jobs)

            if not raw_jobs:
                logger.warning("[Guru] No jobs found on listing page.")
                return result

            all_projects: list[Project] = []

            for i, raw in enumerate(raw_jobs, 1):
                logger.info(
                    "[Guru] Deep-scraping job %d/%d: %s",
                    i, len(raw_jobs), raw["title"][:50],
                )

                project = _scrape_guru_project(page, raw["url"], raw["title"])
                all_projects.append(project)
                result.serious_clients += 1

                if i < len(raw_jobs):
                    _human_delay(1.5, 3.0)

            result.projects = all_projects
            result.new_found = len(all_projects)

        except Exception as exc:
            logger.error("[Guru] Critical error: %s", exc, exc_info=True)
            try:
                page.screenshot(path="logs/guru_error.png")
            except Exception:
                pass
        finally:
            browser.close()

    logger.info(result.summary())
    return result
