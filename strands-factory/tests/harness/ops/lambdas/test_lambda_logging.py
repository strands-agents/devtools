"""The ops Lambdas let the package's INFO lines through (the runtime's root logger is at WARNING)."""

from __future__ import annotations

import logging

import pytest

from strandly.ops.lambdas import configure_logging


@pytest.fixture
def package_logger():
    logger = logging.getLogger("strandly")
    saved = logger.level
    logger.setLevel(logging.NOTSET)
    yield logger
    logger.setLevel(saved)


def test_inside_a_lambda_the_package_logs_at_info(monkeypatch, package_logger):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "fn")
    monkeypatch.delenv("AWS_LAMBDA_LOG_LEVEL", raising=False)
    configure_logging()
    assert package_logger.level == logging.INFO


@pytest.mark.parametrize(
    "env",
    [{}, {"AWS_LAMBDA_FUNCTION_NAME": "fn", "AWS_LAMBDA_LOG_LEVEL": "DEBUG"}],
    ids=["outside a Lambda", "explicit Lambda log level"],
)
def test_otherwise_the_level_is_left_alone(monkeypatch, package_logger, env):
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.delenv("AWS_LAMBDA_LOG_LEVEL", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    configure_logging()
    assert package_logger.level == logging.NOTSET


def test_an_already_configured_level_is_kept(monkeypatch, package_logger):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "fn")
    monkeypatch.delenv("AWS_LAMBDA_LOG_LEVEL", raising=False)
    package_logger.setLevel(logging.DEBUG)
    configure_logging()
    assert package_logger.level == logging.DEBUG
