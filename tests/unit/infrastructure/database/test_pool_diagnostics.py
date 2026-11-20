from types import SimpleNamespace
from typing import Any

import pytest

from src.infrastructure.database import pool_diagnostics
from src.infrastructure.database.pool_diagnostics import (
    _on_checkin,
    _on_checkout,
    _on_detach,
    checkout_site,
)


def _record() -> Any:
    return SimpleNamespace(info={})


def test_checkout_site_names_application_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tests live outside src/, so let the marker match this file to stand in for app code.
    monkeypatch.setattr(pool_diagnostics, "_APP_MARKER", "/tests/")

    def handler_that_queries() -> str:
        return checkout_site()

    site = handler_that_queries()

    assert "test_pool_diagnostics.py" in site
    assert "handler_that_queries" in site


def test_checkout_site_without_application_frames() -> None:
    assert checkout_site() == "unknown (no application frame)"


def test_detach_after_checkout_reports_the_site(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(pool_diagnostics.logger, "warning", warnings.append)
    monkeypatch.setattr(pool_diagnostics, "checkout_site", lambda: "src/telegram/x.py:10 handler")
    record = _record()

    _on_checkout(object(), record, object())
    _on_detach(object(), record)

    assert warnings == [
        "Pooled DB connection was abandoned; it was checked out at: src/telegram/x.py:10 handler"
    ]
    assert record.info == {}


def test_regular_checkin_forgets_the_site(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(pool_diagnostics.logger, "warning", warnings.append)
    record = _record()

    _on_checkout(object(), record, object())
    _on_checkin(object(), record)
    _on_detach(object(), record)

    assert warnings == []
