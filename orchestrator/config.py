"""
config.py v2
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
    line_user_id: str = ""

    # GitHub (for ClaudeCodeAgent)
    github_token: str = ""

    # Trading APIs - Alpaca
    alpaca_email: str = ""
    alpaca_password: str = ""
    alpaca_mfa_secret: str = ""
    alpaca_paper_account_id: str = ""
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"

    # Trading APIs - OANDA (legacy, keeping for compatibility)
    oanda_api_key: str = ""
    oanda_account_id: str = ""
    oanda_environment: str = "practice"

    # Trading APIs - GMO Coin (LIVE - FSA registered Japanese exchange)
    gmo_coin_api_key: str = ""
    gmo_coin_api_secret: str = ""

    # Trading APIs - Bitget (SANDBOX ONLY - demo testing)
    bitget_api_key: str = ""
    bitget_api_secret: str = ""
    bitget_passphrase: str = ""

    # Trading APIs - IBKR (Interactive Brokers)
    ibkr_gateway_url: str = ""       # CP Gateway URL, e.g. https://localhost:5000/v1/api
    ibkr_account_id: str = ""        # Paper or live account ID
    ibkr_paper: bool = True          # True = paper trading

    # Market data
    alpha_vantage_api_key: str = ""

    # Freelance APIs
    smartcat_api_key: str = ""
    smartcat_account_id: str = ""
    gigradar_api_key: str = ""

    # App settings
    port: int = 8000
    db_path: str = "/app/data/orchestrator.db"
    log_level: str = "INFO"
    weekly_report_day: int = 0
    weekly_report_hour: int = 9

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
            github_token=os.getenv("GITHUB_TOKEN", ""),
            alpaca_email=os.getenv("ALPACA_EMAIL", ""),
            alpaca_password=os.getenv("ALPACA_PASSWORD", ""),
            alpaca_mfa_secret=os.getenv("ALPACA_MFA_SECRET", ""),
            alpaca_paper_account_id=os.getenv("ALPACA_PAPER_ACCOUNT_ID", ""),
            alpaca_api_key=os.getenv("ALPACA_API_KEY", ""),
            alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
            alpaca_base_url=os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
            oanda_api_key=os.getenv("OANDA_API_KEY", ""),
            oanda_account_id=os.getenv("OANDA_ACCOUNT_ID", ""),
            oanda_environment=os.getenv("OANDA_ENVIRONMENT", "practice"),
            gmo_coin_api_key=os.getenv("GMO_COIN_API_KEY", ""),
            gmo_coin_api_secret=os.getenv("GMO_COIN_API_SECRET", ""),
            bitget_api_key=os.getenv("BITGET_API_KEY", ""),
            bitget_api_secret=os.getenv("BITGET_API_SECRET", ""),
            bitget_passphrase=os.getenv("BITGET_PASSPHRASE", ""),
            ibkr_gateway_url=os.getenv("IBKR_GATEWAY_URL", ""),
            ibkr_account_id=os.getenv("IBKR_ACCOUNT_ID", ""),
            ibkr_paper=os.getenv("IBKR_PAPER", "true").lower() != "false",
            alpha_vantage_api_key=os.getenv("ALPHA_VANTAGE_API_KEY", ""),
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
