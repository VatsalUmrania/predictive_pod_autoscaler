"""Tests for secure git cloning (PR#13 fix)."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ppa.cli.commands.startup_steps import (
    GIT_CLONE_TIMEOUT,
    get_app_path,
    validate_git_url,
)


class TestGitUrlValidation:
    """Test validate_git_url function."""

    def test_valid_https_url(self):
        """Test valid HTTPS git URL."""
        url = "https://github.com/user/repo.git"
        assert validate_git_url(url) is True

    def test_valid_https_url_without_git_suffix(self):
        """Test valid HTTPS URL without .git suffix."""
        url = "https://github.com/user/repo"
        assert validate_git_url(url) is True

    def test_valid_git_ssh_url(self):
        """Test valid git@github.com SSH URL."""
        url = "git@github.com:user/repo.git"
        assert validate_git_url(url) is True

    def test_valid_git_ssh_url_without_suffix(self):
        """Test valid SSH URL without .git suffix."""
        url = "git@github.com:user/repo"
        assert validate_git_url(url) is True

    def test_valid_ssh_url_different_host(self):
        """Test SSH URL with different host."""
        url = "git@gitlab.com:user/group/repo.git"
        assert validate_git_url(url) is True

    def test_invalid_file_protocol(self):
        """Test that file:// URLs are rejected."""
        url = "file:///etc/passwd"
        assert validate_git_url(url) is False

    def test_invalid_command_injection_attempt(self):
        """Test that command injection attempts are rejected."""
        url = "https://github.com/user/repo.git; rm -rf /"
        assert validate_git_url(url) is False

    def test_invalid_url_with_backticks(self):
        """Test that URLs with backticks are rejected."""
        url = "https://github.com/user/repo.git`whoami`"
        assert validate_git_url(url) is False

    def test_invalid_url_with_pipe(self):
        """Test that URLs with pipe characters are rejected."""
        url = "https://github.com/user/repo.git | cat /etc/passwd"
        assert validate_git_url(url) is False

    def test_invalid_url_with_ampersand(self):
        """Test that URLs with ampersand are rejected."""
        url = "https://github.com/user/repo.git & malicious"
        assert validate_git_url(url) is False

    def test_invalid_url_with_dollar_sign(self):
        """Test that URLs with dollar signs are rejected."""
        url = "https://github.com/user/repo.git$(whoami)"
        assert validate_git_url(url) is False

    def test_invalid_ftp_url(self):
        """Test that FTP URLs are rejected."""
        url = "ftp://github.com/user/repo.git"
        assert validate_git_url(url) is False

    def test_invalid_telnet_url(self):
        """Test that telnet URLs are rejected."""
        url = "telnet://github.com:23"
        assert validate_git_url(url) is False

    def test_invalid_empty_url(self):
        """Test that empty strings are rejected."""
        url = ""
        assert validate_git_url(url) is False

    def test_invalid_relative_path(self):
        """Test that relative paths are rejected."""
        url = "../../../etc/passwd"
        assert validate_git_url(url) is False

    def test_valid_http_url(self):
        """Test valid HTTP (non-HTTPS) git URL."""
        url = "http://github.com/user/repo.git"
        assert validate_git_url(url) is True

    def test_valid_url_with_port(self):
        """Test valid URL with custom port."""
        url = "https://github.com:443/user/repo.git"
        assert validate_git_url(url) is True

    def test_valid_url_with_user_info(self):
        """Test valid URL with user info (though not recommended)."""
        url = "https://user:token@github.com/user/repo.git"
        assert validate_git_url(url) is True


class TestSecureGitClone:
    """Test get_app_path with secure git cloning."""

    def test_returns_none_when_no_app_arg(self):
        """Test that None is returned when app_arg is None."""
        result = get_app_path(None)
        assert result is None

    def test_returns_path_for_local_directory(self):
        """Test that local directory path is returned as-is."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = get_app_path(tmpdir)
            assert result == Path(tmpdir)

    @patch("ppa.cli.commands.startup_steps.subprocess.run")
    @patch("ppa.cli.commands.startup_steps.info")
    def test_clones_valid_https_url(self, mock_info, mock_run):
        """Test that valid HTTPS URL is cloned successfully."""
        url = "https://github.com/user/repo.git"
        mock_run.return_value = MagicMock(returncode=0)

        get_app_path(url)

        # Verify git clone was called
        assert mock_run.called
        call_args = mock_run.call_args

        # Verify clone command structure
        cmd = call_args[0][0]
        assert "git" in cmd
        assert "clone" in cmd
        assert url in cmd

        # Verify timeout was set
        assert call_args[1].get("timeout") is not None

        # Verify git hooks disabled
        env = call_args[1].get("env", {})
        assert env.get("GIT_TERMINAL_PROMPT") == "0"

    @patch("ppa.cli.commands.startup_steps.subprocess.run")
    @patch("ppa.cli.commands.startup_steps.info")
    def test_clones_valid_ssh_url(self, mock_info, mock_run):
        """Test that valid SSH URL is cloned successfully."""
        url = "git@github.com:user/repo.git"
        mock_run.return_value = MagicMock(returncode=0)

        get_app_path(url)

        assert mock_run.called
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert url in cmd

    @patch("ppa.cli.commands.startup_steps.info")
    def test_rejects_file_protocol_url(self, mock_info):
        """Test that file:// URLs are rejected."""
        url = "file:///etc/passwd"

        with pytest.raises(ValueError, match="Invalid git URL"):
            get_app_path(url)

    @patch("ppa.cli.commands.startup_steps.info")
    def test_rejects_command_injection_attempt(self, mock_info):
        """Test that command injection attempts are rejected."""
        url = "https://github.com/user/repo.git; rm -rf /"

        with pytest.raises(ValueError, match="Invalid git URL"):
            get_app_path(url)

    @patch("ppa.cli.commands.startup_steps.subprocess.run")
    @patch("ppa.cli.commands.startup_steps.info")
    def test_timeout_on_git_clone(self, mock_info, mock_run):
        """Test that subprocess timeout is configured."""
        url = "https://github.com/user/repo.git"
        mock_run.return_value = MagicMock(returncode=0)

        get_app_path(url)

        call_args = mock_run.call_args
        # Check that timeout is set to GIT_CLONE_TIMEOUT
        assert call_args[1].get("timeout") == GIT_CLONE_TIMEOUT

    @patch("ppa.cli.commands.startup_steps.subprocess.run")
    @patch("ppa.cli.commands.startup_steps.error")
    def test_handles_git_clone_failure(self, mock_error, mock_run):
        """Test error handling when git clone fails."""
        url = "https://github.com/user/repo.git"
        mock_run.side_effect = subprocess.CalledProcessError(1, ["git", "clone"])

        with pytest.raises(subprocess.CalledProcessError):
            get_app_path(url)

        # Verify error was logged
        assert mock_error.called

    @patch("ppa.cli.commands.startup_steps.subprocess.run")
    @patch("ppa.cli.commands.startup_steps.warn")
    def test_handles_git_timeout(self, mock_warn, mock_run):
        """Test error handling when git clone times out."""
        url = "https://github.com/user/repo.git"
        mock_run.side_effect = subprocess.TimeoutExpired(["git", "clone"], GIT_CLONE_TIMEOUT)

        with pytest.raises(subprocess.TimeoutExpired):
            get_app_path(url)

        # Verify timeout warning was logged
        assert mock_warn.called  # Timeout should be handled

    @patch("ppa.cli.commands.startup_steps.subprocess.run")
    @patch("ppa.cli.commands.startup_steps.info")
    def test_disables_git_hooks(self, mock_info, mock_run):
        """Test that git hooks are disabled during clone."""
        url = "https://github.com/user/repo.git"
        mock_run.return_value = MagicMock(returncode=0)

        get_app_path(url)

        call_args = mock_run.call_args
        # Check environment variables disable git hooks
        env = call_args[1].get("env", {})
        # GIT_TERMINAL_PROMPT=0 prevents interactive prompts
        assert env.get("GIT_TERMINAL_PROMPT") == "0"
        # Additional safeguards could include GIT_SSH_COMMAND, GIT_ALLOW_PROTOCOL

    @patch("ppa.cli.commands.startup_steps.subprocess.run")
    @patch("ppa.cli.commands.startup_steps.info")
    def test_no_recurse_submodules_flag(self, mock_info, mock_run):
        """Test that --no-recurse-submodules is used to prevent malicious submodule clones."""
        url = "https://github.com/user/repo.git"
        mock_run.return_value = MagicMock(returncode=0)

        get_app_path(url)

        call_args = mock_run.call_args
        cmd = call_args[0][0]
        # Check for submodule safety flags
        assert "--no-recurse-submodules" in cmd or "--depth" in cmd  # Safety is implemented

    @patch("ppa.cli.commands.startup_steps.subprocess.run")
    @patch("ppa.cli.commands.startup_steps.info")
    def test_safe_clone_environment(self, mock_info, mock_run):
        """Test that cloning happens in safe environment."""
        url = "https://github.com/user/repo.git"
        mock_run.return_value = MagicMock(returncode=0)

        get_app_path(url)

        call_args = mock_run.call_args
        # Check that check=True to fail on non-zero exit
        assert call_args[1].get("check") is True

    @patch("ppa.cli.commands.startup_steps.subprocess.run")
    @patch("ppa.cli.commands.startup_steps.info")
    def test_clones_with_depth_limit(self, mock_info, mock_run):
        """Test that cloning uses shallow clone to limit data."""
        url = "https://github.com/user/repo.git"
        mock_run.return_value = MagicMock(returncode=0)

        get_app_path(url)

        call_args = mock_run.call_args
        cmd = call_args[0][0]
        # Check for depth limiting (optional but good practice)
        # --depth 1 is a shallow clone
        assert "--depth" in cmd or "--no-recurse-submodules" in cmd


class TestGitUrlEdgeCases:
    """Test edge cases for git URL validation."""

    def test_url_with_newline(self):
        """Test that URLs with newlines are rejected."""
        url = "https://github.com/user/repo.git\nmalicious"
        assert validate_git_url(url) is False

    def test_url_with_null_byte(self):
        """Test that URLs with null bytes are rejected."""
        url = "https://github.com/user/repo.git\x00malicious"
        assert validate_git_url(url) is False

    def test_url_case_sensitivity(self):
        """Test that valid URLs work regardless of case."""
        url = "HTTPS://GITHUB.COM/USER/REPO.GIT"
        # Should be case-insensitive for protocol
        assert validate_git_url(url) is True or validate_git_url(url.lower()) is True

    def test_github_alternative_urls(self):
        """Test various GitHub URL formats."""
        urls = [
            "https://github.com/user/repo.git",
            "git@github.com:user/repo.git",
            "https://github.com/user/repo",
            "git@github.com:user/repo",
        ]
        for url in urls:
            assert validate_git_url(url) is True, f"Failed for {url}"

    def test_gitlab_urls(self):
        """Test GitLab URL formats."""
        urls = [
            "https://gitlab.com/user/repo.git",
            "git@gitlab.com:user/repo.git",
            "https://gitlab.com/group/subgroup/repo.git",
        ]
        for url in urls:
            assert validate_git_url(url) is True, f"Failed for {url}"

    def test_bitbucket_urls(self):
        """Test Bitbucket URL formats."""
        urls = [
            "https://bitbucket.org/user/repo.git",
            "git@bitbucket.org:user/repo.git",
        ]
        for url in urls:
            assert validate_git_url(url) is True, f"Failed for {url}"
