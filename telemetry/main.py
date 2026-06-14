"""Entry point: python -m telemetry.main"""
import logging
import os

from .bot import build_app

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("fastf1").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def main() -> None:
    token = os.getenv("F1_SUBSCRIPTION_TOKEN", "")
    logger.info("F1_SUBSCRIPTION_TOKEN loaded: %s", "YES (len=%d)" % len(token) if token else "NO — radio downloads will fail with 403")
    app = build_app()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
