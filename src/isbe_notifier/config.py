from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://isbe:isbe@localhost:5432/isbe"

    @field_validator("database_url")
    @classmethod
    def _psycopg_scheme(cls, v: str) -> str:
        # Railway/Heroku-style URLs use postgresql://; SQLAlchemy needs the
        # psycopg3 driver spelled out.
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+psycopg://", 1)
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+psycopg://", 1)
        return v

    # Public base URL of the web app, used in email links and push payloads.
    base_url: str = "http://localhost:8000"
    site_name: str = "Illinois Answers Filing Alerts"

    # Poller
    rss_url: str = "https://elections.il.gov/rss/LatestReportsFiled.aspx"
    isbe_base_url: str = "https://elections.il.gov"
    poll_interval_seconds: int = 90
    # ISBE returns 403 to non-browser user agents.
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )

    # Signing for verify/manage/unsubscribe links
    secret_key: str = "dev-secret-change-me"

    # Email: "console" logs instead of sending; "ses" uses Amazon SES.
    email_backend: str = "console"
    email_from: str = "Illinois Answers Filing Alerts <alerts@example.org>"
    aws_region: str = "us-east-2"

    # Web push (generate with: python -m isbe_notifier.notify.push genkeys)
    vapid_private_key: str = ""
    vapid_public_key: str = ""
    vapid_claims_email: str = "mailto:alerts@example.org"

    admin_email: str = ""
    admin_token: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
