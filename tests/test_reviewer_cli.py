"""Tests for the mdp2p reviewer enable/disable/status CLI commands."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mdp2p_client.config as config_module
from bundle import load_public_key, public_key_to_b64
from mdp2p_client.commands import (
    cli_reviewer_disable,
    cli_reviewer_enable,
    cli_reviewer_status,
    cli_setup,
)
from mdp2p_client.config import ClientConfig


@pytest.fixture
def redirect_config_file(tmp_path, monkeypatch):
    """Redirect ClientConfig.save()'s default path so tests never touch ~/.mdp2p."""
    fake = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_FILE", fake)
    return fake


@pytest.fixture
def fresh_config(tmp_path, redirect_config_file):
    cfg = ClientConfig(
        author="test",
        reviewer_dir=str(tmp_path / "reviewer"),
    )
    return cfg


class TestReviewerEnable:
    def test_enables_and_creates_identity(self, fresh_config, tmp_path, capsys):
        rc = cli_reviewer_enable(fresh_config)
        assert rc == 0
        assert fresh_config.reviewer_mode is True

        priv_file = Path(fresh_config.reviewer_dir) / "reviewer.key"
        pub_file = Path(fresh_config.reviewer_dir) / "reviewer.pub"
        assert priv_file.exists()
        assert pub_file.exists()

        out = capsys.readouterr().out
        assert "Reviewer mode is now enabled" in out
        pub_b64 = public_key_to_b64(load_public_key(str(pub_file)))
        assert pub_b64 in out

    def test_records_categories(self, fresh_config):
        rc = cli_reviewer_enable(fresh_config, categories=["tech", "fr"])
        assert rc == 0
        assert fresh_config.reviewer_categories == ["tech", "fr"]

    def test_reuses_identity_on_second_enable(self, fresh_config, tmp_path):
        cli_reviewer_enable(fresh_config)
        first_pub = (tmp_path / "reviewer" / "reviewer.pub").read_bytes()
        cli_reviewer_enable(fresh_config)
        second_pub = (tmp_path / "reviewer" / "reviewer.pub").read_bytes()
        assert first_pub == second_pub, "enable must not regenerate the key"

    def test_already_enabled_message(self, fresh_config, capsys):
        cli_reviewer_enable(fresh_config)
        capsys.readouterr()
        cli_reviewer_enable(fresh_config)
        out = capsys.readouterr().out
        assert "already" in out.lower()


class TestReviewerDisable:
    def test_flips_flag_off(self, fresh_config):
        cli_reviewer_enable(fresh_config)
        rc = cli_reviewer_disable(fresh_config)
        assert rc == 0
        assert fresh_config.reviewer_mode is False

    def test_identity_preserved(self, fresh_config, tmp_path):
        cli_reviewer_enable(fresh_config)
        pub_file = tmp_path / "reviewer" / "reviewer.pub"
        before = pub_file.read_bytes()
        cli_reviewer_disable(fresh_config)
        assert pub_file.exists()
        assert pub_file.read_bytes() == before

    def test_noop_when_already_disabled(self, fresh_config, capsys):
        rc = cli_reviewer_disable(fresh_config)
        assert rc == 0
        out = capsys.readouterr().out
        assert "already disabled" in out


class TestReviewerStatus:
    def test_no_identity_yet(self, fresh_config, capsys):
        rc = cli_reviewer_status(fresh_config)
        assert rc == 0
        out = capsys.readouterr().out
        assert "no identity yet" in out
        assert "Enabled" in out

    def test_enabled_with_identity(self, fresh_config, capsys, tmp_path):
        cli_reviewer_enable(fresh_config, categories=["tech"])
        capsys.readouterr()
        rc = cli_reviewer_status(fresh_config)
        assert rc == 0
        out = capsys.readouterr().out
        pub_b64 = public_key_to_b64(
            load_public_key(str(tmp_path / "reviewer" / "reviewer.pub"))
        )
        assert pub_b64 in out
        assert "tech" in out


class TestSetupWithReviewer:
    def test_reviewer_flag_enables_and_creates_identity(self, tmp_path, redirect_config_file):
        reviewer_dir = tmp_path / "rev"
        # cli_setup accepts a pre-existing config, so seed the reviewer_dir
        # (the default under ~/.mdp2p/reviewer would otherwise leak outside tmp).
        cfg = ClientConfig(
            author="seed",
            reviewer_dir=str(reviewer_dir),
        )
        rc = cli_setup(
            cfg,
            author="alice",
            reviewer=True,
            reviewer_categories=["science"],
        )
        assert rc == 0
        assert cfg.reviewer_mode is True
        assert cfg.reviewer_categories == ["science"]
        assert (reviewer_dir / "reviewer.key").exists()

    def test_setup_without_reviewer_flag_leaves_mode_off(self, tmp_path, redirect_config_file):
        cfg = ClientConfig(
            author="seed",
            reviewer_dir=str(tmp_path / "rev"),
        )
        rc = cli_setup(cfg, author="alice")
        assert rc == 0
        assert cfg.reviewer_mode is False
        assert not (tmp_path / "rev" / "reviewer.key").exists()
