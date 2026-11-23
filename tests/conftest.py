import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


@pytest.fixture(autouse=True)
def _no_llm_backoff_sleep(monkeypatch):
    monkeypatch.setattr("digest.summarizer._backoff_sleep", lambda seconds: None)
