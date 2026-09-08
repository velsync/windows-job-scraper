"""Browser-opening behavior for normal launches vs automated gates."""

import webbrowser

from jobscraper.launcher.lifecycle import open_dashboard


def test_open_dashboard_opens_normally(monkeypatch):
    monkeypatch.delenv("WJS_SUPPRESS_BROWSER_OPEN", raising=False)
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)

    url = open_dashboard(60630, "ticket-value")

    assert url == "http://127.0.0.1:60630/#bootstrap=ticket-value"
    assert opened == [url]


def test_open_dashboard_is_inert_when_gate_suppression_is_set(monkeypatch):
    monkeypatch.setenv("WJS_SUPPRESS_BROWSER_OPEN", "1")
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)

    url = open_dashboard(60630, "ticket-value")

    assert url == "http://127.0.0.1:60630/#bootstrap=ticket-value"
    assert opened == []
