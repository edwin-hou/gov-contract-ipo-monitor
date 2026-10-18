"""Nonfinite settings cannot disable a price/capitalization gate or start work."""
import math

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from contract_ipo_monitor.cli import app
from contract_ipo_monitor.config import Settings
from contract_ipo_monitor.service import MonitorService


@pytest.mark.parametrize("field", ["max_price", "max_market_cap"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_qualification_thresholds_are_runtime_errors(field, value):
    settings = Settings(sec_user_agent="Monitor test@example.org", **{field: value})
    assert any("finite" in error for error in settings.runtime_errors())


@pytest.mark.parametrize("command", ["check-config", "run-once", "run"])
def test_cli_rejects_nonfinite_threshold_before_building_service(command, monkeypatch):
    settings = Settings(sec_user_agent="Monitor test@example.org", max_price=math.nan)
    monkeypatch.setattr(Settings, "from_env", staticmethod(lambda _: settings))
    monkeypatch.setattr(MonitorService, "build_default", staticmethod(
        lambda _: pytest.fail("Invalid settings started a collector or scheduler")))
    result = CliRunner().invoke(app, [command])
    assert result.exit_code == 2
    assert "finite" in result.output


@pytest.mark.parametrize("field", ["source_timeout_seconds", "sec_interval_seconds",
                                   "usaspending_interval_seconds", "sam_interval_seconds",
                                   "smtp_poll_seconds", "discourse_interval_seconds",
                                   "market_interval_seconds", "report_interval_seconds", "quote_max_age_hours"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_timing_settings_are_rejected_by_typed_configuration(field, value):
    with pytest.raises(ValidationError):
        Settings(sec_user_agent="Monitor test@example.org", **{field: value})


def test_finite_positive_thresholds_remain_valid():
    assert Settings(sec_user_agent="Monitor test@example.org", max_price=5.0,
                    max_market_cap=300_000_000.0).runtime_errors() == []
