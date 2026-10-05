from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel


class Settings(BaseModel):
    database_path: Path = Path("data/monitor.db")
    evidence_archive_path: Path = Path("data/evidence")
    sec_user_agent: str = ""
    sam_api_key: str = ""
    twelve_data_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: str = "starttls"
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_sender: str = ""
    smtp_recipients: tuple[str, ...] = ()
    smtp_enabled: bool = False
    discourse_enabled: bool = True
    watch_companies: tuple[str, ...] = ("Anduril", "SpaceX", "OpenAI", "Anthropic", "Databricks", "Stripe")
    news_feed_urls: tuple[str, ...] = ()
    youtube_video_urls: tuple[str, ...] = ("https://youtu.be/0BE2AAOlYWI",)
    youtube_api_key: str = ""
    reddit_enabled: bool = True
    discourse_interval_seconds: int = 3600
    discourse_max_companies: int = 12
    source_timeout_seconds: int = 600
    sec_max_pages: int = 3
    sec_interval_seconds: int = 30
    usaspending_interval_seconds: int = 300
    usaspending_initial_lookback_days: int = 7
    usaspending_overlap_days: int = 2
    usaspending_max_pages: int = 250
    sam_interval_seconds: int = 21600
    smtp_poll_seconds: int = 5
    max_price: float = 5.0
    max_market_cap: float = 300_000_000
    quote_max_age_hours: int = 24
    health_host: str = "0.0.0.0"
    health_port: int = 8080
    enabled_sec_forms: tuple[str, ...] = ("S-1", "S-1/A", "F-1", "F-1/A", "S-11", "S-11/A", "1-A", "1-A/A", "8-K", "RW", "AW", "EFFECT", "424B4")

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env") -> "Settings":
        if env_file:
            load_dotenv(env_file, override=False)
        recipients = tuple(x.strip() for x in os.getenv("SMTP_RECIPIENTS", "").split(",") if x.strip())
        forms = tuple(x.strip() for x in os.getenv("SEC_FORMS", "S-1,S-1/A,F-1,F-1/A,S-11,S-11/A,1-A,1-A/A,8-K,RW,AW,EFFECT,424B4").split(",") if x.strip())
        def boolean(name: str, default: bool) -> bool:
            value = os.getenv(name, str(default)).strip().lower()
            if value not in {"true", "false", "1", "0", "yes", "no"}:
                raise ValueError(f"{name} must be true or false")
            return value in {"true", "1", "yes"}
        def values(name: str, default: str = "") -> tuple[str, ...]:
            return tuple(x.strip() for x in os.getenv(name, default).split(",") if x.strip())
        return cls(
            database_path=Path(os.getenv("DATABASE_PATH", "data/monitor.db")),
            evidence_archive_path=Path(os.getenv("EVIDENCE_ARCHIVE_PATH", "data/evidence")),
            sec_user_agent=os.getenv("SEC_USER_AGENT", ""),
            sam_api_key=os.getenv("SAM_API_KEY", ""),
            twelve_data_api_key=os.getenv("TWELVE_DATA_API_KEY", ""),
            smtp_host=os.getenv("SMTP_HOST", ""),
            smtp_port=int(os.getenv("SMTP_PORT", "587")),
            smtp_security=os.getenv("SMTP_SECURITY", "starttls"),
            smtp_username=os.getenv("SMTP_USERNAME", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            smtp_sender=os.getenv("SMTP_SENDER", ""),
            smtp_recipients=recipients,
            smtp_enabled=boolean("SMTP_ENABLED", False),
            discourse_enabled=boolean("DISCOURSE_ENABLED", True),
            watch_companies=values("WATCH_COMPANIES", "Anduril,SpaceX,OpenAI,Anthropic,Databricks,Stripe"),
            news_feed_urls=values("NEWS_FEED_URLS"),
            youtube_video_urls=values("YOUTUBE_VIDEO_URLS", "https://youtu.be/0BE2AAOlYWI"),
            youtube_api_key=os.getenv("YOUTUBE_API_KEY", ""),
            reddit_enabled=boolean("REDDIT_ENABLED", True),
            discourse_interval_seconds=int(os.getenv("DISCOURSE_INTERVAL_SECONDS", "3600")),
            discourse_max_companies=int(os.getenv("DISCOURSE_MAX_COMPANIES", "12")),
            source_timeout_seconds=int(os.getenv("SOURCE_TIMEOUT_SECONDS", "600")),
            sec_max_pages=int(os.getenv("SEC_MAX_PAGES", "3")),
            sec_interval_seconds=int(os.getenv("SEC_INTERVAL_SECONDS", "30")),
            usaspending_interval_seconds=int(os.getenv("USASPENDING_INTERVAL_SECONDS", "300")),
            usaspending_initial_lookback_days=int(os.getenv("USASPENDING_INITIAL_LOOKBACK_DAYS", "7")),
            usaspending_overlap_days=int(os.getenv("USASPENDING_OVERLAP_DAYS", "2")),
            usaspending_max_pages=int(os.getenv("USASPENDING_MAX_PAGES", "250")),
            sam_interval_seconds=int(os.getenv("SAM_INTERVAL_SECONDS", "21600")),
            smtp_poll_seconds=int(os.getenv("SMTP_POLL_SECONDS", "5")),
            max_price=float(os.getenv("MAX_PRICE", "5")),
            max_market_cap=float(os.getenv("MAX_MARKET_CAP", "300000000")),
            quote_max_age_hours=int(os.getenv("QUOTE_MAX_AGE_HOURS", "24")),
            health_host=os.getenv("HEALTH_HOST", "0.0.0.0"),
            health_port=int(os.getenv("HEALTH_PORT", "8080")),
            enabled_sec_forms=forms,
        )

    def runtime_errors(self) -> list[str]:
        errors: list[str] = []
        if not self.sec_user_agent or "@" not in self.sec_user_agent:
            errors.append("SEC_USER_AGENT must identify the application and include a contact email.")
        if self.smtp_enabled and not self.smtp_host:
            errors.append("SMTP_HOST is required.")
        if self.smtp_enabled and not self.smtp_sender:
            errors.append("SMTP_SENDER is required.")
        if self.smtp_enabled and not self.smtp_recipients:
            errors.append("SMTP_RECIPIENTS must contain at least one address.")
        if self.smtp_security not in {"starttls", "ssl", "none"}:
            errors.append("SMTP_SECURITY must be starttls, ssl, or none.")
        if self.smtp_port <= 0 or self.smtp_port > 65535:
            errors.append("SMTP_PORT must be a valid TCP port.")
        if self.usaspending_initial_lookback_days < 1:
            errors.append("USASPENDING_INITIAL_LOOKBACK_DAYS must be at least 1.")
        if self.usaspending_overlap_days < 0:
            errors.append("USASPENDING_OVERLAP_DAYS cannot be negative.")
        if self.usaspending_max_pages < 1:
            errors.append("USASPENDING_MAX_PAGES must be at least 1.")
        if self.max_price <= 0 or self.max_market_cap <= 0 or self.quote_max_age_hours <= 0:
            errors.append("Qualification thresholds and quote age must be positive.")
        for name in ("sec_interval_seconds", "usaspending_interval_seconds", "sam_interval_seconds", "smtp_poll_seconds", "discourse_interval_seconds", "source_timeout_seconds", "sec_max_pages"):
            if getattr(self, name) < 1:
                errors.append(f"{name.upper()} must be positive.")
        if not 1 <= self.discourse_max_companies <= 50:
            errors.append("DISCOURSE_MAX_COMPANIES must be between 1 and 50.")
        if not self.enabled_sec_forms:
            errors.append("SEC_FORMS must contain at least one form.")
        return errors

    def prepare_paths(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.evidence_archive_path.mkdir(parents=True, exist_ok=True)
