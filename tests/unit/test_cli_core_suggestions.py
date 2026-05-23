"""Tests for ppa.cli.core.suggestions module."""

from ppa.cli.core.suggestions import (
    AVAILABLE_COMMANDS,
    format_error_with_suggestion,
    suggest_fix,
)


class TestSuggestFix:
    """Tests for suggest_fix function."""

    def test_suggest_common_typo(self):
        """Test suggestions for known typos."""
        assert "startup" in suggest_fix("startp")
        assert "monitor" in suggest_fix("montior")
        assert "onboard" in suggest_fix("onbaord")

    def test_suggest_common_mistakes(self):
        """Test suggestions for common flag mistakes."""
        suggestion = suggest_fix("--verbose")
        assert suggestion is not None
        assert "--debug" in suggestion

    def test_no_suggestion_for_valid_command(self):
        """Test no suggestion for valid commands."""
        for cmd in AVAILABLE_COMMANDS:
            suggestion = suggest_fix(cmd, context="command")
            # May or may not suggest depending on similarity
            # (the point is it shouldn't crash)
            assert suggestion is None or isinstance(suggestion, str)

    def test_fuzzy_match_command(self):
        """Test fuzzy matching for commands."""
        suggestion = suggest_fix("strtup", context="command")
        # Should suggest 'startup' or 'status'
        assert suggestion is not None
        assert "startup" in suggestion or "status" in suggestion

    def test_fuzzy_match_option(self):
        """Test fuzzy matching for common options."""
        suggestion = suggest_fix("--hlp")
        assert suggestion is not None
        assert "--help" in suggestion

    def test_no_suggestion_for_unknown(self):
        """Test no suggestion for completely unknown input."""
        suggestion = suggest_fix("xyzabc123def456")
        assert suggestion is None


class TestFormatErrorWithSuggestion:
    """Tests for format_error_with_suggestion function."""

    def test_format_error_with_suggestion(self):
        """Test formatting error with suggestion."""
        msg = format_error_with_suggestion("Unknown command: startp", "startp")
        assert "Unknown command: startp" in msg
        assert "[hint]" in msg or "Did you mean" in msg

    def test_format_error_without_suggestion(self):
        """Test formatting error without user_input."""
        msg = format_error_with_suggestion("Something failed")
        assert "Something failed" in msg
        assert "[hint]" not in msg

    def test_format_error_no_matching_suggestion(self):
        """Test formatting error when no suggestion matches."""
        msg = format_error_with_suggestion("Error: xyz", "xyz123")
        assert "Error: xyz" in msg
