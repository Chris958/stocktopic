from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from stocktopic.config import Settings, load_dotenv
from stocktopic.providers import TushareClient

APP_DIR = Path(__file__).resolve().parent.parent


def main() -> None:
    load_dotenv(APP_DIR / ".env")
    settings = Settings.from_env(require_secrets=False)
    client = TushareClient(
        settings.tushare_token,
        mcp_url=settings.tushare_mcp_url,
    )
    trade_date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    rows = client.trade_calendar(trade_date, trade_date)
    if not rows:
        raise RuntimeError("Tushare交易日历探测未返回数据")
    print(
        json.dumps(
            {
                "status": "ok",
                "transport": client.transport,
                "trade_calendar_rows": len(rows),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {"status": "failed", "error": str(error)},
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from error
