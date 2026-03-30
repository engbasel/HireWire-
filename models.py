"""
HireWire — Data Models
Typed dataclasses for structured data flow between modules.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ClientInfo:
    """Extracted client information from a project page."""
    name: str = ""
    hiring_rate: int = 0          # 0-100 percentage
    total_projects: int = 0       # How many projects the client has posted
    country: str = ""
    verification_status: str = ""  # e.g., "verified", "unverified"
    join_date: str = ""           # Registration date text (e.g. "11 أكتوبر 2023")
    is_new_client: bool = False   # True if joined within NEW_CLIENT_DAYS


@dataclass
class Project:
    """A single project with full details (from Mostaql or Nafezly)."""
    title: str
    url: str
    description: str = ""
    budget: str = ""
    time_posted: str = ""
    skills: list[str] = field(default_factory=list)
    proposals_count: str = ""
    client: ClientInfo = field(default_factory=ClientInfo)
    source: str = "mostaql"  # "mostaql", "nafezly", "pph", or "guru"


@dataclass
class ScrapingResult:
    """Summary of a scraping cycle."""
    total_on_page: int = 0
    already_seen: int = 0
    new_found: int = 0
    serious_clients: int = 0      # Passed hiring rate filter
    new_clients_kept: int = 0     # Kept due to being newly registered
    filtered_out: int = 0         # Failed hiring rate filter
    scraped_at: datetime = field(default_factory=datetime.now)
    projects: list[Project] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"📊 Scraping Summary:\n"
            f"   Total on page: {self.total_on_page}\n"
            f"   Already seen: {self.already_seen}\n"
            f"   New found: {self.new_found}\n"
            f"   Serious clients (hiring > 0%): {self.serious_clients}\n"
            f"   New clients kept (joined recently): {self.new_clients_kept}\n"
            f"   Filtered out: {self.filtered_out}"
        )
