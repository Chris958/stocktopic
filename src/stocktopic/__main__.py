from __future__ import annotations

import logging

import uvicorn

from .ai_compat import install_ai_relay_compat
from .config import Settings
from .data_resilience import install_data_resilience
from .theme_policy import install_theme_policy
from .theme_taxonomy import install_theme_taxonomy


def main() -> None:
    install_data_resilience()
    install_ai_relay_compat()
    install_theme_taxonomy()
    install_theme_policy()
    from .api import create_app

    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
