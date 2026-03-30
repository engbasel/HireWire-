"""
HireWire — Main Orchestrator & Scheduler
Ties all components together into a self-sustaining automation loop.
"""

import time
import signal
import sys
import schedule

from config import (
    logger,
    validate_config,
    MOSTAQL_URL,
    NAFEZLY_URL,
    PPH_URL,
    GURU_URL,
    AI_CRITERIA,
    INTERVAL_MINUTES,
)
from database import init_db, is_processed, mark_as_processed, cleanup_old_entries, get_stats
from scraper import scrape_mostaql, scrape_nafezly, scrape_pph, scrape_guru
from ai_agent import analyze_projects
from notifier import send_report, send_alert, send_startup_ping


# ---------------------------------------------------------------------------
# Graceful Shutdown
# ---------------------------------------------------------------------------
_running = True


def _shutdown_handler(signum: int, frame: object) -> None:
    """Handle Ctrl+C gracefully."""
    global _running
    _running = False
    logger.info("\n🛑 Shutdown signal received. Finishing current cycle...")


signal.signal(signal.SIGINT, _shutdown_handler)
signal.signal(signal.SIGTERM, _shutdown_handler)


# ---------------------------------------------------------------------------
# Main Job
# ---------------------------------------------------------------------------
def job() -> None:
    """
    Execute one full automation cycle:
    1. Scrape Mostaql listing page
    2. Visit each project page to check client hiring rate
    3. Filter against SQLite memory (skip already-seen)
    4. Send serious projects to Gemini AI
    5. Deliver the report to Telegram
    6. Save processed URLs to database
    """
    logger.info("")
    logger.info("━" * 60)
    logger.info("🔄 Starting Automation Cycle: %s", time.strftime("%Y-%m-%d %H:%M"))
    logger.info("━" * 60)

    try:
        # --- Step 1: Scrape all platforms ---
        logger.info("[Main] 🔍 Scraping Mostaql...")
        mostaql_result = scrape_mostaql(MOSTAQL_URL)

        logger.info("[Main] 🔍 Scraping Nafezly...")
        nafezly_result = scrape_nafezly(NAFEZLY_URL)

        logger.info("[Main] 🔍 Scraping PeoplePerHour...")
        pph_result = scrape_pph(PPH_URL)

        logger.info("[Main] 🔍 Scraping Guru...")
        guru_result = scrape_guru(GURU_URL)

        # Merge results from all platforms
        all_projects = mostaql_result.projects + nafezly_result.projects + pph_result.projects + guru_result.projects
        total_on_page = mostaql_result.total_on_page + nafezly_result.total_on_page + pph_result.total_on_page + guru_result.total_on_page

        logger.info(
            "[Main] Combined: %d Mostaql + %d Nafezly + %d PPH + %d Guru = %d total",
            len(mostaql_result.projects), len(nafezly_result.projects),
            len(pph_result.projects), len(guru_result.projects), len(all_projects),
        )

        if total_on_page == 0:
            logger.warning("[Main] No projects found on any platform. Sites may be down.")
            return

        # --- Step 2: Filter against memory ---
        new_projects = []
        already_seen = 0
        for project in all_projects:
            if is_processed(project.url):
                already_seen += 1
            else:
                new_projects.append(project)

        logger.info(
            "[Main] %d total serious projects, %d are NEW (unseen), %d already in DB",
            len(all_projects), len(new_projects), already_seen,
        )

        if not new_projects:
            logger.info("[Main] No new serious projects to analyze. Going back to sleep. 💤")
            return

        # --- Step 3: AI Analysis ---
        ai_report = analyze_projects(new_projects, AI_CRITERIA)

        # --- Step 4: Deliver & Save ---
        if ai_report and len(ai_report) > 20:
            success = send_report(ai_report)

            if success:
                # Mark as processed ONLY after successful delivery
                for project in new_projects:
                    mark_as_processed(
                        url=project.url,
                        title=project.title,
                        hiring_rate=project.client.hiring_rate,
                    )
                logger.info(
                    "[Main] ✅ Cycle complete — %d projects processed and saved.",
                    len(new_projects),
                )
            else:
                logger.error(
                    "[Main] ❌ Failed to send Telegram report. Projects NOT marked as processed."
                )
        else:
            logger.info("[Main] AI determined no projects matched your exact criteria.")

    except Exception as exc:
        logger.error("[Main] ❌ Critical error in job cycle: %s", exc, exc_info=True)
        # Try to send error alert via Telegram
        try:
            send_alert(
                f"⚠️ Alert: Mostaql AI Agent Error\n\n"
                f"Error: {str(exc)[:500]}\n"
                f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"The script will retry on the next scheduled cycle."
            )
        except Exception:
            pass  # If Telegram itself is down, just log


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------
def main() -> None:
    """Initialize everything and start the scheduler loop."""
    print()
    print(" ⚡ HireWire — Autonomous Freelance AI Scout Starting...")
    print(" ━" * 30 + "\n")

    # Validate configuration
    if not validate_config():
        sys.exit(1)

    # Initialize database
    init_db()

    # Cleanup old entries (30+ days)
    cleanup_old_entries(30)

    # Show DB stats
    stats = get_stats()
    logger.info("📊 Database: %d total entries, %d in last 7 days", stats["total_entries"], stats["last_7_days"])

    # Verify Telegram connectivity
    logger.info("[Main] Sending startup ping to Telegram...")
    if send_startup_ping():
        logger.info("[Main] ✅ Telegram connectivity confirmed!")
    else:
        logger.warning("[Main] ⚠️  Could not reach Telegram. Check your BOT_TOKEN and CHAT_ID.")

    # Run the job immediately on startup
    logger.info("[Main] Running initial scan...")
    job()

    # Schedule future runs
    schedule.every(INTERVAL_MINUTES).minutes.do(job)
    logger.info("[Main] ⏳ Scheduled to run every %d minutes. Keep this terminal open.", INTERVAL_MINUTES)
    logger.info("[Main] Press Ctrl+C to stop.\n")

    # Keep-alive loop
    while _running:
        schedule.run_pending()
        time.sleep(30)  # Check every 30s for minute-level precision

    logger.info("🛑 HireWire stopped gracefully. Goodbye!")


if __name__ == "__main__":
    main()
