"""Tests for the `mdp2p language <code>` CLI subcommand."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mdp2p_client.config as config_module
from mdp2p_client.commands import cli_language
from mdp2p_client.config import ClientConfig


@pytest.fixture
def redirect_config_file(tmp_path, monkeypatch):
    fake = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_FILE", fake)
    return fake


@pytest.fixture
def cfg(redirect_config_file):
    return ClientConfig(author="alice", language="fr")


class TestCliLanguage:
    def test_sets_valid_language(self, cfg, capsys):
        rc = cli_language(cfg, "en")
        assert rc == 0
        assert cfg.language == "en"
        out = capsys.readouterr().out
        # config_saved key should be translated in English after load.
        assert "en" in out or "English" in out or "saved" in out.lower()

    def test_rejects_unknown_language(self, cfg, capsys):
        rc = cli_language(cfg, "xx")
        assert rc != 0
        assert cfg.language == "fr"  # unchanged
        out = capsys.readouterr().out
        assert "xx" in out or "unknown" in out.lower()

    def test_persists_to_config_file(self, cfg, redirect_config_file):
        rc = cli_language(cfg, "zh")
        assert rc == 0
        reloaded = ClientConfig.load(redirect_config_file)
        assert reloaded is not None
        assert reloaded.language == "zh"

    def test_all_supported_languages_accepted(self, cfg):
        for code in ("fr", "en", "zh", "ar", "hi"):
            rc = cli_language(cfg, code)
            assert rc == 0
            assert cfg.language == code
