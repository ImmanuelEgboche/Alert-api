"""Production refuses unsafe settings; LOG_LEVEL actually filters logs."""
import logging

import pytest
import structlog
from pydantic import ValidationError
from structlog.testing import capture_logs

import src.main  # noqa: F401  (applies the structlog configuration)
from src.config import Settings, get_settings

GOOD_KEY = "k" * 48


def make(**kw) -> Settings:
    # _env_file=None: don't let the developer's .env leak into the test
    return Settings(_env_file=None, **kw)


@pytest.mark.parametrize("key", ["change-me-in-production", "your-secret-key-change-in-production"])
def test_production_rejects_placeholder_secret(key):
    with pytest.raises(ValidationError, match="published placeholder"):
        make(environment="production", secret_key=key)


def test_production_rejects_short_secret():
    with pytest.raises(ValidationError, match="at least 32"):
        make(environment="production", secret_key="k" * 31)


def test_production_rejects_debug():
    with pytest.raises(ValidationError, match="DEBUG must be false"):
        make(environment="production", secret_key=GOOD_KEY, debug=True)


def test_production_reports_every_problem_at_once():
    with pytest.raises(ValidationError) as e:
        make(environment="production", secret_key="short", debug=True)
    assert "DEBUG" in str(e.value) and "SECRET_KEY" in str(e.value)


def test_production_with_safe_settings_starts():
    s = make(environment="production", secret_key=GOOD_KEY, debug=False)
    assert s.environment == "production"


@pytest.mark.parametrize("env", ["development", "staging"])
def test_non_production_allows_dev_defaults(env):
    s = make(environment=env, debug=True)          # default placeholder key
    assert s.debug


def test_log_level_is_case_insensitive_and_validated():
    assert make(log_level="info").log_level == "INFO"
    with pytest.raises(ValidationError):
        make(log_level="LOUD")


def test_structlog_uses_configured_log_level():
    level = logging.getLevelName(get_settings().log_level)
    assert structlog.get_config()["wrapper_class"] is structlog.make_filtering_bound_logger(level)


def test_debug_logs_are_filtered_at_info():
    """Before the fix every trade logged a debug line, whatever LOG_LEVEL said."""
    if get_settings().log_level == "DEBUG":
        pytest.skip("LOG_LEVEL=DEBUG in this environment")
    logger = structlog.get_logger()
    with capture_logs() as logs:
        logger.debug("price_update")
        logger.info("kept")
    assert [entry["event"] for entry in logs] == ["kept"]


def test_replay_feed_requires_a_file():
    with pytest.raises(ValueError, match="REPLAY_FILE"):
        Settings(_env_file=None, feed_source="replay")


def test_production_refuses_replay_feed():
    with pytest.raises(ValueError, match="FEED_SOURCE"):
        Settings(_env_file=None, environment="production", secret_key="x" * 40,
                 feed_source="replay", replay_file="r.jsonl.gz")

