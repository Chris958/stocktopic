from __future__ import annotations

import logging

import uvicorn

from .config import Settings
from .data_resilience import install_data_resilience


def main() -> None:
    install_data_resilience()
    from .api import create_app

    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
