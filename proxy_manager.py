"""
HireWire — Proxy & Anti-Detection Manager

Handles:
    1. Proxy rotation (residential, datacenter, or free)
    2. User-Agent rotation (realistic browser fingerprints)
    3. Viewport randomization
    4. Stealth mode integration (playwright-stealth)

Setup:
    - Set PROXY_LIST in .env (comma-separated proxy URLs)
    - Or leave empty to run without proxies (direct connection)
    - Format: protocol://user:pass@host:port or protocol://host:port
    - Example: http://user:pass@proxy1.example.com:8080,socks5://proxy2:1080
"""

import os
import random
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("hirewire")


# ---------------------------------------------------------------------------
# Realistic User-Agent Pool (Chrome on Windows/Mac, 2024-2025)
# ---------------------------------------------------------------------------
_USER_AGENTS: list[str] = [
    # Chrome 125 — Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Chrome 124 — Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 125 — macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Chrome 123 — Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Edge 125 — Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    # Chrome 125 — Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Firefox 126 — Windows (for diversity)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    # Chrome 122 — macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

# Viewport presets that look like real monitors
_VIEWPORTS: list[dict[str, int]] = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 720},
    {"width": 1600, "height": 900},
    {"width": 2560, "height": 1440},
]

# Locale pool (match target platform regions)
_LOCALES: list[str] = [
    "ar-SA", "ar-EG", "en-US", "en-GB",
]


# ---------------------------------------------------------------------------
# ProxyManager
# ---------------------------------------------------------------------------
@dataclass
class ProxyManager:
    """
    Manages proxy rotation, User-Agent cycling, and browser fingerprint
    randomization for anti-detection.

    Usage:
        mgr = ProxyManager.from_env()
        context_options = mgr.get_context_options(locale="ar-SA")
        # Pass context_options to browser.new_context(**context_options)
    """

    proxies: list[str] = field(default_factory=list)
    _index: int = field(default=0, repr=False)

    # ── Statistics ──
    total_requests: int = 0
    blocked_count: int = 0

    # ── Core Methods ──

    @classmethod
    def from_env(cls) -> "ProxyManager":
        """
        Load proxy list from PROXY_LIST environment variable.
        Comma-separated. Empty = no proxies (direct connection).
        """
        raw = os.getenv("PROXY_LIST", "")
        proxies = [p.strip() for p in raw.split(",") if p.strip()]

        if proxies:
            logger.info("[Proxy] Loaded %d proxies from PROXY_LIST", len(proxies))
        else:
            logger.info("[Proxy] No proxies configured — using direct connection")

        return cls(proxies=proxies)

    @property
    def has_proxies(self) -> bool:
        """Whether any proxies are configured."""
        return len(self.proxies) > 0

    def next_proxy(self) -> dict[str, str] | None:
        """
        Get the next proxy in round-robin rotation.
        Returns None if no proxies are configured.

        Format for Playwright: {"server": "http://host:port"}
        If proxy has auth:     {"server": "...", "username": "...", "password": "..."}
        """
        if not self.proxies:
            return None

        proxy_url = self.proxies[self._index % len(self.proxies)]
        self._index += 1

        # Parse proxy URL for Playwright format
        # Supported: http://host:port, http://user:pass@host:port, socks5://...
        proxy_config: dict[str, str] = {"server": proxy_url}

        # Extract username:password if present
        if "@" in proxy_url:
            # protocol://user:pass@host:port
            try:
                auth_part = proxy_url.split("://")[1].split("@")[0]
                host_part = proxy_url.split("@")[1]
                protocol = proxy_url.split("://")[0]

                if ":" in auth_part:
                    username, password = auth_part.split(":", 1)
                    proxy_config = {
                        "server": f"{protocol}://{host_part}",
                        "username": username,
                        "password": password,
                    }
            except (IndexError, ValueError):
                pass  # Malformed URL — use as-is

        return proxy_config

    def random_user_agent(self) -> str:
        """Return a random realistic User-Agent string."""
        return random.choice(_USER_AGENTS)

    def random_viewport(self) -> dict[str, int]:
        """Return a random realistic viewport size."""
        base = random.choice(_VIEWPORTS)
        # Add slight jitter (±10px) to avoid exact-match fingerprinting
        return {
            "width": base["width"] + random.randint(-10, 10),
            "height": base["height"] + random.randint(-10, 10),
        }

    def get_context_options(
        self,
        locale: str | None = None,
        rotate_proxy: bool = True,
    ) -> dict:
        """
        Generate a full set of Playwright browser context options
        with randomized fingerprint for anti-detection.

        Usage:
            opts = mgr.get_context_options(locale="ar-SA")
            context = browser.new_context(**opts)
        """
        self.total_requests += 1

        options: dict = {
            "user_agent": self.random_user_agent(),
            "viewport": self.random_viewport(),
            "locale": locale or random.choice(_LOCALES),
        }

        # Add proxy if available
        if rotate_proxy and self.has_proxies:
            proxy = self.next_proxy()
            if proxy:
                options["proxy"] = proxy

        return options

    def report_blocked(self) -> None:
        """Record a blocked/captcha event for monitoring."""
        self.blocked_count += 1
        logger.warning(
            "[Proxy] Block detected! Total blocks: %d / %d requests (%.1f%%)",
            self.blocked_count,
            self.total_requests,
            (self.blocked_count / max(self.total_requests, 1)) * 100,
        )

    def stats(self) -> dict:
        """Return proxy usage statistics."""
        return {
            "proxies_configured": len(self.proxies),
            "total_requests": self.total_requests,
            "blocked_count": self.blocked_count,
            "block_rate": f"{(self.blocked_count / max(self.total_requests, 1)) * 100:.1f}%",
        }


# ---------------------------------------------------------------------------
# Stealth Helper
# ---------------------------------------------------------------------------
def apply_stealth(page) -> None:  # noqa: ANN001 — Page type from playwright
    """
    Apply stealth patches to a Playwright page to avoid bot detection.
    Uses playwright-stealth v2 API (Stealth class).
    """
    try:
        from playwright_stealth import Stealth  # type: ignore[import-untyped]
        stealth = Stealth()
        stealth.apply_stealth_sync(page)
        logger.debug("[Stealth] Applied playwright-stealth v2 patches")
    except ImportError:
        # Manual stealth: override navigator.webdriver
        try:
            page.add_init_script("""
                // Override navigator.webdriver
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined,
                });

                // Override chrome.runtime to avoid detection
                window.chrome = {
                    runtime: {},
                };

                // Override permissions
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) =>
                    parameters.name === 'notifications'
                        ? Promise.resolve({ state: Notification.permission })
                        : originalQuery(parameters);

                // Override plugins length
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5],
                });

                // Override languages
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en', 'ar'],
                });
            """)
            logger.debug("[Stealth] Applied manual stealth patches (playwright-stealth not installed)")
        except Exception as exc:
            logger.debug("[Stealth] Could not apply stealth patches: %s", exc)
    except Exception as exc:
        logger.debug("[Stealth] Error applying stealth: %s. Using manual fallback.", exc)
        try:
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
            """)
        except Exception:
            pass
