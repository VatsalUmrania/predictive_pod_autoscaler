"""Tests for ppa.cli.core.validators module."""

from pathlib import Path

import pytest

from ppa.cli.core.errors import ValidationError
from ppa.cli.core.validators import (
    validate_app_name,
    validate_filepath,
    validate_horizon,
    validate_namespace,
)


class TestValidateAppName:
    """Tests for validate_app_name."""

    def test_valid_app_name_simple(self):
        """Test valid simple app name."""
        assert validate_app_name("my-app") == "my-app"

    def test_valid_app_name_with_numbers(self):
        """Test valid app name with numbers."""
        assert validate_app_name("app-v2") == "app-v2"

    def test_invalid_app_name_empty(self):
        """Test empty app name raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            validate_app_name("")
        assert "cannot be empty" in str(exc_info.value)

    def test_invalid_app_name_too_long(self):
        """Test app name > 63 characters raises ValidationError."""
        long_name = "a" * 64
        with pytest.raises(ValidationError) as exc_info:
            validate_app_name(long_name)
        assert "too long" in str(exc_info.value)

    def test_invalid_app_name_uppercase(self):
        """Test uppercase in app name raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            validate_app_name("MyApp")
        assert "Invalid app name" in str(exc_info.value)

    def test_invalid_app_name_underscore(self):
        """Test underscore in app name raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            validate_app_name("my_app")
        assert "Invalid app name" in str(exc_info.value)

    def test_invalid_app_name_starts_with_hyphen(self):
        """Test name starting with hyphen raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            validate_app_name("-myapp")
        assert "Invalid app name" in str(exc_info.value)

    def test_invalid_app_name_ends_with_hyphen(self):
        """Test name ending with hyphen raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            validate_app_name("myapp-")
        assert "Invalid app name" in str(exc_info.value)


class TestValidateNamespace:
    """Tests for validate_namespace."""

    def test_valid_namespace(self):
        """Test valid namespace."""
        assert validate_namespace("default") == "default"
        assert validate_namespace("ppa-system") == "ppa-system"

    def test_invalid_namespace_empty(self):
        """Test empty namespace raises ValidationError."""
        with pytest.raises(ValidationError):
            validate_namespace("")

    def test_invalid_namespace_too_long(self):
        """Test namespace > 63 characters raises ValidationError."""
        with pytest.raises(ValidationError):
            validate_namespace("a" * 64)


class TestValidateHorizon:
    """Tests for validate_horizon."""

    def test_valid_horizon(self):
        """Test valid horizons."""
        assert validate_horizon(1) == 1
        assert validate_horizon(3) == 3
        assert validate_horizon(24) == 24

    def test_valid_horizon_string_conversion(self):
        """Test string converts to int."""
        assert validate_horizon("3") == 3

    def test_invalid_horizon_zero(self):
        """Test horizon 0 raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            validate_horizon(0)
        assert "must be >= 1" in str(exc_info.value)

    def test_invalid_horizon_negative(self):
        """Test negative horizon raises ValidationError."""
        with pytest.raises(ValidationError):
            validate_horizon(-1)

    def test_invalid_horizon_too_large(self):
        """Test horizon > 24 raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            validate_horizon(25)
        assert "too large" in str(exc_info.value)

    def test_invalid_horizon_non_numeric_string(self):
        """Test non-numeric string raises ValidationError."""
        with pytest.raises(ValidationError):
            validate_horizon("abc")


class TestValidateFilepath:
    """Tests for validate_filepath."""

    def test_valid_filepath_not_must_exist(self, tmp_path):
        """Test valid filepath when must_exist=False."""
        path = tmp_path / "file.txt"
        result = validate_filepath(str(path), must_exist=False)
        assert isinstance(result, Path)

    def test_valid_filepath_must_exist(self, tmp_path):
        """Test valid filepath when must_exist=True."""
        file = tmp_path / "test.txt"
        file.write_text("test")
        result = validate_filepath(str(file), must_exist=True)
        assert result == file

    def test_invalid_filepath_must_exist(self, tmp_path):
        """Test missing filepath when must_exist=True raises ValidationError."""
        path = tmp_path / "missing.txt"
        with pytest.raises(ValidationError) as exc_info:
            validate_filepath(str(path), must_exist=True)
        assert "not found" in str(exc_info.value)

    def test_filepath_accepts_path_object(self, tmp_path):
        """Test filepath accepts Path objects."""
        file = tmp_path / "test.txt"
        file.write_text("test")
        result = validate_filepath(file, must_exist=True)
        assert result == file
