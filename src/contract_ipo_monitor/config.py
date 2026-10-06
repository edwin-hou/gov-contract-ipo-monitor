from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field


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
    reddit_access_token: str = Field(default="", repr=False)
    reddit_client_id: str = ""
    reddit_client_secret: str = Field(default="", repr=False)
    reddit_refresh_token: str = Field(default="", repr=False)
    reddit_posts_per_company: int = 5
    reddit_comments_per_post: int = 10
    reddit_max_comments_per_run: int = 60
    forum_feed_urls: tuple[str, ...] = ("https://forum.valuepickr.com/latest.rss",)
    forums_enabled: bool = True
    hacker_news_enabled: bool = True
    markets_enabled: bool = True
    listed_discovery_enabled: bool = True
    listed_discovery_max_new_ciks: int = 5
    listed_discovery_max_candidates: int = 25
    watch_symbols: tuple[str, ...] = ()
    market_interval_seconds: int = 3600
    fundamental_refresh_days: int = 7
    fundamental_max_age_days: int = 120
    price_max_age_business_days: int = 1
    discourse_interval_seconds: int = 3600
    discourse_max_companies: int = 30
    source_timeout_seconds: int = 600
    sec_max_pages: int = 3
    sec_max_document_bytes: int = 20 * 1024 * 1024
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
            reddit_access_token=os.getenv("REDDIT_ACCESS_TOKEN", ""),
            reddit_client_id=os.getenv("REDDIT_CLIENT_ID", ""),
            reddit_client_secret=os.getenv("REDDIT_CLIENT_SECRET", ""),
            reddit_refresh_token=os.getenv("REDDIT_REFRESH_TOKEN", ""),
            reddit_posts_per_company=int(os.getenv("REDDIT_POSTS_PER_COMPANY", "5")),
            reddit_comments_per_post=int(os.getenv("REDDIT_COMMENTS_PER_POST", "10")),
            reddit_max_comments_per_run=int(os.getenv("REDDIT_MAX_COMMENTS_PER_RUN", "60")),
            forum_feed_urls=values("FORUM_FEED_URLS", "https://forum.valuepickr.com/latest.rss"),
            forums_enabled=boolean("FORUMS_ENABLED", True),
            hacker_news_enabled=boolean("HACKER_NEWS_ENABLED", True),
            markets_enabled=boolean("MARKETS_ENABLED", True),
            listed_discovery_enabled=boolean("LISTED_DISCOVERY_ENABLED", True),
            listed_discovery_max_new_ciks=int(os.getenv("LISTED_DISCOVERY_MAX_NEW_CIKS", "5")),
            listed_discovery_max_candidates=int(os.getenv("LISTED_DISCOVERY_MAX_CANDIDATES", "25")),
            watch_symbols=values("WATCH_SYMBOLS"),
            market_interval_seconds=int(os.getenv("MARKET_INTERVAL_SECONDS", "3600")),
            fundamental_refresh_days=int(os.getenv("FUNDAMENTAL_REFRESH_DAYS", "7")),
            fundamental_max_age_days=int(os.getenv("FUNDAMENTAL_MAX_AGE_DAYS", "120")),
            price_max_age_business_days=int(os.getenv("PRICE_MAX_AGE_BUSINESS_DAYS", "1")),
            discourse_interval_seconds=int(os.getenv("DISCOURSE_INTERVAL_SECONDS", "3600")),
            discourse_max_companies=int(os.getenv("DISCOURSE_MAX_COMPANIES", "30")),
            source_timeout_seconds=int(os.getenv("SOURCE_TIMEOUT_SECONDS", "600")),
            sec_max_pages=int(os.getenv("SEC_MAX_PAGES", "3")),
            sec_max_document_bytes=int(os.getenv("SEC_MAX_DOCUMENT_BYTES", str(20 * 1024 * 1024))),
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
        for name in ("sec_interval_seconds", "usaspending_interval_seconds", "sam_interval_seconds", "smtp_poll_seconds", "discourse_interval_seconds", "source_timeout_seconds", "sec_max_pages", "market_interval_seconds", "fundamental_refresh_days", "fundamental_max_age_days"):
            if getattr(self, name) < 1:
                errors.append(f"{name.upper()} must be positive.")
        if not 1 <= self.discourse_max_companies <= 50:
            errors.append("DISCOURSE_MAX_COMPANIES must be between 1 and 50.")
        if not 0 <= self.price_max_age_business_days <= 5:
            errors.append("PRICE_MAX_AGE_BUSINESS_DAYS must be between 0 and 5.")
        if not 1 <= self.listed_discovery_max_new_ciks <= 5 or not 1 <= self.listed_discovery_max_candidates <= 25:
            errors.append("Listed discovery bounds must be 1-5 new issuer CIKs and 1-25 active candidates.")
        if not 1 <= self.reddit_posts_per_company <= 10 or not 0 <= self.reddit_comments_per_post <= 50 or not 0 <= self.reddit_max_comments_per_run <= 300:
            errors.append("Reddit collection bounds are invalid (posts 1-10, comments/post 0-50, comments/run 0-300).")
        if (self.reddit_client_secret or self.reddit_refresh_token) and not self.reddit_client_id:
            errors.append("REDDIT_CLIENT_ID is required with Reddit client secret or refresh token.")
        if any(len(value) > 8192 or any(character.isspace() for character in value) for value in (self.reddit_access_token, self.reddit_client_id, self.reddit_client_secret, self.reddit_refresh_token)):
            errors.append("Reddit credential format is invalid.")
        if len(self.forum_feed_urls) > 30 or len(self.news_feed_urls) > 30 or len(self.youtube_video_urls) > 30:
            errors.append("At most 30 URLs per forum, news, or video source list.")
        if self.watch_symbols:
            from .universe import default_universe
            unknown = set(self.watch_symbols) - {instrument.symbol for instrument in default_universe()}
            if unknown:
                errors.append("WATCH_SYMBOLS contains unreviewed instruments: " + ", ".join(sorted(unknown)))
        if not 1 <= self.sec_max_document_bytes <= 50 * 1024 * 1024:
            errors.append("SEC_MAX_DOCUMENT_BYTES must be positive and cannot exceed 50 MiB (52428800 bytes).")
        if not self.enabled_sec_forms:
            errors.append("SEC_FORMS must contain at least one form.")
        return errors

    def prepare_paths(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.evidence_archive_path.mkdir(parents=True, exist_ok=True)
