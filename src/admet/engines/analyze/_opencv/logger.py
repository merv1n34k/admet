"""
Simple logging for Droplet Measurement System
"""

import logging
import sys

# Single shared handler to avoid duplicates
_initialized = False


def get_logger(name: str) -> logging.Logger:
    """
    Get logger with given name.

    Args:
        name: Logger name (appears in output)

    Returns:
        Logger instance

    Usage:
        logger = get_logger("Pipeline")
        logger.info("Starting...")
        # Output: [12:34:56 - Pipeline - INFO] Starting...
    """
    global _initialized

    logger = logging.getLogger(name)

    # Initialize root handler once
    if not _initialized:
        root = logging.getLogger()
        root.setLevel(logging.INFO)

        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.INFO)
        handler.setFormatter(
            logging.Formatter(
                "[%(asctime)s - %(name)s - %(levelname)s] %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        root.addHandler(handler)
        _initialized = True

    return logger
