import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import unit_compare as uc


def test_default_limit(monkeypatch):
    monkeypatch.delenv("FB_TIMEOUT", raising=False)
    assert uc.run_timeout() == uc.TIMEOUT


def test_zero_means_no_limit(monkeypatch):
    monkeypatch.setenv("FB_TIMEOUT", "0")
    assert uc.run_timeout() is None


def test_explicit_limit(monkeypatch):
    monkeypatch.setenv("FB_TIMEOUT", "3600")
    assert uc.run_timeout() == 3600
