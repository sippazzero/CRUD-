"""Placeholder worker process for the separately deployed queue consumer."""

import asyncio
import logging

from app.config import get_settings
from app.logging_config import configure_logging

configure_logging(get_settings().log_level)
logger: logging.Logger = logging.getLogger(__name__)


async def main() -> None:
    """Keep the worker container available until queue processing is implemented."""
    logger.info("Worker process started; queue processing is not implemented yet")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
