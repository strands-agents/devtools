"""The ops Lambda handlers; ``configure_logging`` is the setup they share."""

from __future__ import annotations

import logging
import os

#: The package whose INFO lines the Lambdas emit.
_PACKAGE_LOGGER = "strandly"


def configure_logging() -> None:
    """Let this package's INFO lines through inside a Lambda.

    The Lambda Python runtime's root logger defaults to WARNING, which drops every ``logger.info``.
    An explicit ``AWS_LAMBDA_LOG_LEVEL`` (the function's logging config) wins; outside a Lambda
    this does nothing.
    """
    if not os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get("AWS_LAMBDA_LOG_LEVEL"):
        return
    logger = logging.getLogger(_PACKAGE_LOGGER)
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)
