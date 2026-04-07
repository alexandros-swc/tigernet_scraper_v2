"""
Configuration settings for the TigerNet scraper.
"""

from dataclasses import dataclass, field


@dataclass
class Settings:
    """Scraper configuration."""

    # TigerNet base URL
    base_url: str = "https://tigernet.princeton.edu"

    # API endpoints
    users_endpoint: str = "/frontoffice/api/users"
    profile_endpoint_template: str = "/users/{my_user_id}/users/{target_user_id}"

    # Pagination
    per_page: int = 100  # Increased from 50 — fewer listing requests needed
    max_pages: int | None = None

    # Rate limiting (seconds between requests)
    request_delay: float = 0.5   # Profile fetches — reduced from 1.5s after testing
    listing_delay: float = 0.5   # Listing pages — reduced from 1.0s after testing

    # Parallelism
    num_tabs: int = 4  # Number of browser tabs for parallel profile fetching

    # Retry settings
    max_retries: int = 3
    retry_backoff_base: float = 2.0  # Exponential backoff base (2, 4, 8 seconds)
    request_timeout: int = 30  # Seconds

    # Output
    output_path: str = "output/tigernet_alumni.csv"
    progress_file: str = "output/progress.json"

    # Browser settings
    headless: bool = False

    # Your user ID (extracted from JWT during auth)
    my_user_id: str | None = None