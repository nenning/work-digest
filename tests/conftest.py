import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


@pytest.fixture(autouse=True)
def _no_llm_backoff_sleep(monkeypatch):
    monkeypatch.setattr("digest.summarizer._backoff_sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _no_config_annotate(monkeypatch):
    """main() must never rewrite or look up page titles for the real digest/config.yaml during tests."""
    from unittest.mock import MagicMock
    mock = MagicMock()
    monkeypatch.setattr("digest.main.annotate_config", mock)
    return mock


@pytest.fixture(autouse=True)
def _reset_anonymizer():
    from digest import anonymizer
    anonymizer.configure(None)
    yield
    anonymizer.configure(None)
