"""
config.py v1
Environment variable configuration for the 2AI Orchestrator.
All secrets come from Railway environment variables (never hardcoded).
"""
import os
from dataclasses import dataclass


@dataclass
class Config:
    # Claude API
    anthropic_api_key: str = ""
    claude_haiku_model: str = "claude-haiku-4-5-20251001"
    claude_sonnet_model: str = "claude-sonnet-4-6"
    claude_opus_model: str = "claude-opus-4-8"

    # LINE Bot
    line_channel_access_token: str = ""
    line_channel_secret: str = ""
    line_user_id: str = ""  # Takuma-san's LINE user ID for push messages

    # Trading APIs
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"  # paper trading by default
    oanda_api_key: str = ""
    oanda_account_id: str = ""
    oanda_environment: str = "practice"  # practice -> live after verification

    # Freelance APIs
    smartcat_api_key: str = ""
    smartcat_account_id: str = ""
    gigradar_api_key: str = ""  # Upwork via GigRadar

    # App settings
    port: int = 8000
    db_path: str = "/app/data/orchestrator.db"
    log_level: str = "INFO"
    weekly_report_day: int = 0   # 0=Monday
    weekly_report_hour: int = 9  # 09:00 JST

    # Cost limits (USD/month)
    max_monthly_claude_cost: float = 10.0
    max_monthly_infra_cost: float = 10.0

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            claude_haiku_model=os.getenv("CLAUDE_HAIKU_MODEL", "claude-haiku-4-5-20251001"),
            claude_sonnet_model=os.getenv("CLAUDE_SONNET_MODEL", "claude-sonnet-4-6"),
            claude_opus_model=os.getenv("CLAUDE_OPUS_MODEL", "claude-opus-4-8"),
            line_channel_access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""),
            line_channel_secret=os.getenv("LINE_CHANNEL_SECRET", ""),
            line_user_id=os.getenv("LINE_USER_ID", ""),
            alpaca_api_key=os.getenv("ALPACA_API_KEY", ""),
            alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
            alpaca_base_url=os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
            oanda_api_key=os.getenv("OANDA_API_KEY", ""),
            oanda_account_id=os.getenv("OANDA_ACCOUNT_ID", ""),
            oanda_environment=os.getenv("OANDA_ENVIRONMENT", "practice"),
            smartcat_api_key=os.getenv("SMARTCAT_API_KEY", ""),
            smartcat_account_id=os.getenv("SMARTCAT_ACCOUNT_ID", ""),
            gigradar_api_key=os.getenv("GIGRADAR_API_KEY", ""),
            port=int(os.getenv("PORT", "8000")),
            db_path=os.getenv("DB_PATH", "/app/data/orchestrator.db"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            weekly_report_day=int(os.getenv("WEEKLY_REPORT_DAY", "0")),
            weekly_report_hour=int(os.getenv("WEEKLY_REPORT_HOUR", "9")),
            max_monthly_claude_cost=float(os.getenv("MAX_MONTHLY_CLAUDE_COST", "10.0")),
            max_monthly_infra_cost=float(os.getenv("MAX_MONTHLY_INFRA_COST", "10.0")),
        )


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"Required environment variable '{key}' is not set.")
    return val
