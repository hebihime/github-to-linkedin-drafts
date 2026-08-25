from __future__ import annotations

from src.github_client import read_token


def test_read_token_prefers_env(monkeypatch) -> None:
    monkeypatch.setenv("GH_PAT", "pat-from-env")
    monkeypatch.setenv("GITHUB_TOKEN", "actions-token")
    assert read_token() == "pat-from-env"


def test_read_token_falls_back_to_github_token(monkeypatch) -> None:
    monkeypatch.delenv("GH_PAT", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "actions-token")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert read_token() == "actions-token"
