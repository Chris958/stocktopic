from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_dotenv(path: Path = Path(".env")) -> None:
    """Small dotenv loader so production does not depend on python-dotenv."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True, slots=True)
class Settings:
    tushare_token: str
    db_path: Path
    archive_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    market_timezone: str = "Asia/Shanghai"
    quote_interval_minutes: int = 5
    cluster_interval_minutes: int = 15
    stale_after_seconds: int = 120
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5.5"
    wecom_corp_id: str = ""
    wecom_agent_id: str = ""
    wecom_secret: str = ""
    wecom_to_user: str = "@all"
    admin_username: str = "admin"
    admin_password: str = ""
    app_api_token: str = ""
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, require_secrets: bool = True) -> Settings:
        load_dotenv()
        token = _required("TUSHARE_TOKEN") if require_secrets else os.getenv("TUSHARE_TOKEN", "")
        db_path = Path(os.getenv("STOCKTOPIC_DB_PATH", "./data/stocktopic.sqlite3"))
        archive_dir = Path(os.getenv("STOCKTOPIC_ARCHIVE_DIR", "./data/archive"))
        admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
        app_api_token = os.getenv("APP_API_TOKEN", "").strip()
        if require_secrets and not admin_password:
            raise RuntimeError("Missing required environment variable: ADMIN_PASSWORD")
        if require_secrets and not app_api_token:
            raise RuntimeError("Missing required environment variable: APP_API_TOKEN")
        return cls(
            tushare_token=token,
            db_path=db_path,
            archive_dir=archive_dir,
            host=os.getenv("STOCKTOPIC_HOST", "127.0.0.1"),
            port=int(os.getenv("STOCKTOPIC_PORT", "8765")),
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            openai_base_url=(
                os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
                or "https://api.openai.com/v1"
            ),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.5").strip(),
            wecom_corp_id=os.getenv("WECOM_CORP_ID", "").strip(),
            wecom_agent_id=os.getenv("WECOM_AGENT_ID", "").strip(),
            wecom_secret=os.getenv("WECOM_SECRET", "").strip(),
            wecom_to_user=os.getenv("WECOM_TO_USER", "@all").strip(),
            admin_username=os.getenv("ADMIN_USERNAME", "admin").strip(),
            admin_password=admin_password,
            app_api_token=app_api_token,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )

    def ensure_directories(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def validate_integrations(self) -> list[str]:
        warnings: list[str] = []
        if not self.openai_api_key:
            warnings.append("OPENAI_API_KEY missing: AI naming and news explanation disabled")
        wecom = (self.wecom_corp_id, self.wecom_agent_id, self.wecom_secret)
        if any(wecom) and not all(wecom):
            warnings.append("WeCom configuration incomplete: push disabled")
        elif not all(wecom):
            warnings.append("WeCom not configured: push disabled")
        return warnings


def generate_secret() -> str:
    return secrets.token_urlsafe(32)
