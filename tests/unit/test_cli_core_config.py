"""Tests for ppa.cli.core.config module."""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from ppa.cli.core.config import (
    CLIConfig,
    get_cli_config_dir,
    get_cli_config_path,
    load_cli_config,
    save_cli_config,
)
from ppa.cli.core.errors import ConfigError


class TestCLIConfig:
    """Tests for CLIConfig dataclass."""

    def test_default_config(self):
        """Test CLIConfig with defaults."""
        config = CLIConfig()
        assert config.default_app_name == "demo-app"
        assert config.default_namespace == "default"
        assert config.default_horizon == 3
        assert config.prometheus_url == "http://localhost:9090"
        assert config.interactive is False
        assert config.debug is False

    def test_config_from_dict(self):
        """Test creating config from dictionary."""
        data = {
            "default_app_name": "my-app",
            "default_namespace": "prod",
            "default_horizon": 5,
        }
        config = CLIConfig.from_dict(data)
        assert config.default_app_name == "my-app"
        assert config.default_namespace == "prod"
        assert config.default_horizon == 5
        assert config.debug is False  # Default not in dict

    def test_config_to_dict(self):
        """Test converting config to dictionary."""
        config = CLIConfig(default_app_name="test-app", default_horizon=2)
        data = config.to_dict()
        assert data["default_app_name"] == "test-app"
        assert data["default_horizon"] == 2


class TestConfigPaths:
    """Tests for config path functions."""

    def test_get_cli_config_dir_exists(self):
        """Test get_cli_config_dir creates directory."""
        config_dir = get_cli_config_dir()
        assert config_dir.exists()
        assert config_dir.is_dir()

    def test_get_cli_config_path(self):
        """Test get_cli_config_path returns ~/.ppa/cli.yaml."""
        config_path = get_cli_config_path()
        assert config_path.name == "cli.yaml"
        assert ".ppa" in str(config_path)


class TestConfigIO:
    """Tests for config loading/saving."""

    def test_load_config_nonexistent(self):
        """Test loading config when file doesn't exist returns defaults."""
        with TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "cli.yaml"
            config = load_cli_config(config_path)
            assert config.default_app_name == "demo-app"
            assert config.default_namespace == "default"

    def test_save_and_load_config(self):
        """Test saving and loading config."""
        with TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "cli.yaml"

            # Save
            original = CLIConfig(
                default_app_name="my-app",
                default_namespace="prod",
                default_horizon=5,
                debug=True,
            )
            save_cli_config(original, config_path)
            assert config_path.exists()

            # Load
            loaded = load_cli_config(config_path)
            assert loaded.default_app_name == "my-app"
            assert loaded.default_namespace == "prod"
            assert loaded.default_horizon == 5
            assert loaded.debug is True

    def test_load_config_invalid_yaml(self):
        """Test loading invalid YAML raises ConfigError."""
        with TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "cli.yaml"
            config_path.write_text("invalid: yaml: content:")

            with pytest.raises(ConfigError) as exc_info:
                load_cli_config(config_path)
            assert "Invalid YAML" in str(exc_info.value)

    def test_save_config_creates_directory(self):
        """Test saving config creates directory if needed."""
        with TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "nested" / "cli.yaml"
            config = CLIConfig()
            save_cli_config(config, config_path)
            assert config_path.exists()
            assert config_path.parent.exists()

    def test_config_preserves_optional_fields(self):
        """Test config preserves optional fields like kubeconfig."""
        with TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "cli.yaml"
            original = CLIConfig(kubeconfig="/home/user/.kube/config")
            save_cli_config(original, config_path)
            loaded = load_cli_config(config_path)
            assert loaded.kubeconfig == "/home/user/.kube/config"
