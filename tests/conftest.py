import pytest

from arb import config


@pytest.fixture(autouse=True)
def example_config(monkeypatch):
    """Tests always read the tracked config.example.toml, never the local config.toml."""
    monkeypatch.setattr(config, "CONFIG_PATH", config.EXAMPLE_CONFIG_PATH)
