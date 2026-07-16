"""
test_loadshedding.py — Tests for the load shedding stage source
================================================================
Covers the ESP fetch (parse, failure modes, range validation), the stage
resolution order (manual > esp > default), and the badge color bands.
Network access is always mocked; no test hits the real ESP API.
"""

from typing import Any

import requests

import loadshedding
from loadshedding import (
    _fetch_esp_stage_uncached,
    _resolve_stage,
    _stage_color,
    _STAGE_COLORS,
    fetch_esp_stage,
)


class _FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, payload: Any, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error

    def raise_for_status(self) -> None:
        """Raise the configured HTTP error, if any."""
        if self._error is not None:
            raise self._error

    def json(self) -> Any:
        """Return the configured JSON payload."""
        return self._payload


def _payload(stage: Any) -> dict:
    """Build an ESP status payload reporting the given eskom stage."""
    return {"status": {"eskom": {"stage": stage}}}


# ---------------------------------------------------------------------------
# fetch_esp_stage
# ---------------------------------------------------------------------------

def test_fetch_returns_none_without_api_key(monkeypatch) -> None:
    """No API key means None, and no network call is attempted."""
    monkeypatch.setattr(loadshedding, "_get_api_key", lambda: None)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("requests.get must not be called without a key")

    monkeypatch.setattr(loadshedding.requests, "get", _boom)
    assert _fetch_esp_stage_uncached() is None


def test_fetch_parses_stage_from_payload(monkeypatch) -> None:
    """A valid payload yields the stage as an int."""
    monkeypatch.setattr(loadshedding, "_get_api_key", lambda: "k")
    monkeypatch.setattr(
        loadshedding.requests, "get",
        lambda *a, **kw: _FakeResponse(_payload("3")),
    )
    assert _fetch_esp_stage_uncached() == 3


def test_fetch_returns_none_on_http_error(monkeypatch) -> None:
    """An HTTP error status degrades to None instead of raising."""
    monkeypatch.setattr(loadshedding, "_get_api_key", lambda: "k")
    monkeypatch.setattr(
        loadshedding.requests, "get",
        lambda *a, **kw: _FakeResponse(
            _payload(2), error=requests.HTTPError("403 Forbidden")
        ),
    )
    assert _fetch_esp_stage_uncached() is None


def test_fetch_returns_none_on_network_error(monkeypatch) -> None:
    """Timeouts and connection failures never propagate."""
    monkeypatch.setattr(loadshedding, "_get_api_key", lambda: "k")

    def _timeout(*args: Any, **kwargs: Any) -> None:
        raise requests.Timeout("timed out")

    monkeypatch.setattr(loadshedding.requests, "get", _timeout)
    assert _fetch_esp_stage_uncached() is None


def test_fetch_returns_none_on_malformed_payload(monkeypatch) -> None:
    """A payload missing the expected keys degrades to None."""
    monkeypatch.setattr(loadshedding, "_get_api_key", lambda: "k")
    monkeypatch.setattr(
        loadshedding.requests, "get",
        lambda *a, **kw: _FakeResponse({"unexpected": True}),
    )
    assert _fetch_esp_stage_uncached() is None


def test_fetch_rejects_out_of_range_stage(monkeypatch) -> None:
    """Stages outside 0..8 are rejected as invalid data."""
    monkeypatch.setattr(loadshedding, "_get_api_key", lambda: "k")
    for bad in (9, -1, 100):
        monkeypatch.setattr(
            loadshedding.requests, "get",
            lambda *a, _bad=bad, **kw: _FakeResponse(_payload(_bad)),
        )
        assert _fetch_esp_stage_uncached() is None


def test_fetch_rejects_non_numeric_stage(monkeypatch) -> None:
    """A non-numeric stage value degrades to None."""
    monkeypatch.setattr(loadshedding, "_get_api_key", lambda: "k")
    monkeypatch.setattr(
        loadshedding.requests, "get",
        lambda *a, **kw: _FakeResponse(_payload("unknown")),
    )
    assert _fetch_esp_stage_uncached() is None


def test_cached_wrapper_exposes_wrapped(monkeypatch) -> None:
    """The undecorated function is reachable via __wrapped__ for testing."""
    monkeypatch.setattr(loadshedding, "_get_api_key", lambda: None)
    assert callable(fetch_esp_stage.__wrapped__)
    assert fetch_esp_stage.__wrapped__() is None


# ---------------------------------------------------------------------------
# Stage resolution
# ---------------------------------------------------------------------------

def test_resolve_stage_manual_beats_esp() -> None:
    """A manual override wins even when ESP reports a different stage."""
    assert _resolve_stage(4, 2) == (4, "manual")


def test_resolve_stage_auto_uses_esp() -> None:
    """Without an override, the live ESP stage is used."""
    assert _resolve_stage(None, 2) == (2, "esp")


def test_resolve_stage_defaults_to_zero() -> None:
    """With no override and no ESP data, assume stage 0."""
    assert _resolve_stage(None, None) == (0, "default")


# ---------------------------------------------------------------------------
# Badge colors
# ---------------------------------------------------------------------------

def test_stage_color_bands() -> None:
    """Stages 0..8 map to green / amber / red / dark red bands."""
    assert _stage_color(0) == _STAGE_COLORS["green"]
    for stage in (1, 2):
        assert _stage_color(stage) == _STAGE_COLORS["amber"]
    for stage in (3, 4):
        assert _stage_color(stage) == _STAGE_COLORS["red"]
    for stage in (5, 6, 7, 8):
        assert _stage_color(stage) == _STAGE_COLORS["dark_red"]


def test_stage_color_out_of_range_is_severe() -> None:
    """Unexpected values fall back to the severe band, never understated."""
    assert _stage_color(9) == _STAGE_COLORS["dark_red"]
    assert _stage_color(42) == _STAGE_COLORS["dark_red"]
