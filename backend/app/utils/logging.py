from __future__ import annotations

import logging


def get_logger(name: str) -> logging.Logger:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return logging.getLogger(name)
