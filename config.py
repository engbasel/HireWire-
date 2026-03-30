"""
HireWire — Configuration & Logging Setup
Centralized configuration loader. Validates all required env vars at startup.
"""

import os
import sys
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
load_dotenv()

# ---------------------------------------------------------------------------
# Required Credentials
# ---------------------------------------------------------------------------
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Application Settings (edit these as needed)
# ---------------------------------------------------------------------------
MOSTAQL_URL: str = "https://mostaql.com/projects?category=development&sort=latest"
NAFEZLY_URL: str = "https://nafezly.com/projects?specialize=development&page=1"
PPH_URL: str = "https://www.peopleperhour.com/freelance-jobs"
GURU_URL: str = "https://www.guru.com/d/jobs/c/programming-development/"

AI_CRITERIA: str = "مشاريع برمجة وتطوير الويب أو تطبيقات الهواتف الذكية أو أتمتة الأعمال."

# Minimum client hiring rate to consider a project "serious" (0-100)
MIN_HIRING_RATE: int = 1  # Filter out 0% hiring rate clients

# Schedule interval in minutes (check frequently to catch new projects fast)
INTERVAL_MINUTES: int = 5

# Gemini model
GEMINI_MODEL: str = "gemini-2.5-flash"

# Database
DB_NAME: str = "mostaql_memory.db"

# Scraper settings
SCRAPER_TIMEOUT_MS: int = 60000
SCRAPER_MIN_DELAY: float = 2.0
SCRAPER_MAX_DELAY: float = 5.0
MAX_PROJECTS_PER_RUN: int = 30  # Safety cap per cycle

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)


def setup_logging() -> logging.Logger:
    """Configure rotating file logger + console output."""
    logger = logging.getLogger("hirewire")
    logger.setLevel(logging.DEBUG)

    # Prevent duplicate handlers on re-import
    if logger.handlers:
        return logger

    # Console handler (clean output for ordinary users)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    
    # Custom formatter class to add colors based on log level
    class ColorFormatter(logging.Formatter):
        grey = "\x1b[38;20m"
        green = "\x1b[32;20m"
        yellow = "\x1b[33;20m"
        red = "\x1b[31;20m"
        bold_red = "\x1b[31;1m"
        reset = "\x1b[0m"
        
        FORMATS = {
            logging.DEBUG: grey + "%(asctime)s │ %(message)s" + reset,
            logging.INFO: "\x1b[36m%(asctime)s\x1b[0m │ %(message)s", # Cyan time
            logging.WARNING: yellow + "%(asctime)s │ ⚠️ %(message)s" + reset,
            logging.ERROR: red + "%(asctime)s │ ❌ %(message)s" + reset,
            logging.CRITICAL: bold_red + "%(asctime)s │ 💥 %(message)s" + reset
        }

        def format(self, record):
            log_fmt = self.FORMATS.get(record.levelno)
            formatter = logging.Formatter(log_fmt, datefmt="%H:%M")
            return formatter.format(record)

    console.setFormatter(ColorFormatter())

    # File handler (DEBUG+, rotating 5MB × 3 backups)
    file_handler = RotatingFileHandler(
        LOG_DIR / "agent.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


# Initialize logger on import
logger = setup_logging()


# ---------------------------------------------------------------------------
# Startup Validation
# ---------------------------------------------------------------------------
def validate_config() -> bool:
    """Validate that all required credentials are set. Returns True if valid."""
    missing: list[str] = []

    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        logger.error(
            "❌ Missing required environment variables: %s", ", ".join(missing)
        )
        logger.error("   Please fill in your .env file and restart.")
        return False

    logger.info("✅ Configuration validated successfully.")
    logger.info("   Model: %s", GEMINI_MODEL)
    logger.info("   Mostaql: %s", MOSTAQL_URL)
    logger.info("   Nafezly: %s", NAFEZLY_URL)
    logger.info("   PPH:     %s", PPH_URL)
    logger.info("   Guru:    %s", GURU_URL)
    logger.info("   Min Hiring Rate: %d%%", MIN_HIRING_RATE)
    logger.info("   Schedule: Every %d minutes", INTERVAL_MINUTES)
    return True
