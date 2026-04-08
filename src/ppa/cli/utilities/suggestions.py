"""Smart error suggestions — "did you mean" and typo detection."""

from __future__ import annotations

from difflib import get_close_matches

# Suggestion database 

COMMON_TYPOS = {
    "startp": "startup",
    "deploy": "deploy",
    "onbaord": "onboard",
    "montior": "monitor",
    "moddel": "model",
    "data": "data",
    "statu": "status",
    "tooolbox": "toolbox",
    "folw": "follow",
    "opertor": "operator",
}

COMMON_MISTAKES = {
    "--help": "Use 'ppa <command> --help' for command-specific help",
    "--verbose": "Use '--debug' for detailed output",
    "-v": "Use '--version' for version or '-d' for debug",
}

AVAILABLE_COMMANDS = [
    "startup",
    "deploy",
    "onboard",
    "monitor",
    "model",
    "data",
    "status",
    "toolbox",
    "follow",
    "operator",
]

# Suggestion logic

def suggest_fix(user_input: str, context: str | None = None) -> str | None:
    """Suggest a fix for a command or option.

    Args:
        user_input: User-entered command or option
        context: Where the input was used (e.g., "command" or "flag")

    Returns:
        Suggestion string, or None if no suggestion found

    Examples:
        >>> suggest_fix("startp")
        "Did you mean 'startup'?"
        >>> suggest_fix("montior")
        "Did you mean 'monitor'?"
    """
    # Check direct typos
    if user_input in COMMON_TYPOS:
        return f"Did you mean '{COMMON_TYPOS[user_input]}'?"

    # Check common mistakes
    if user_input in COMMON_MISTAKES:
        return COMMON_MISTAKES[user_input]

    # Fuzzy match against commands
    if context == "command":
        matches = get_close_matches(user_input, AVAILABLE_COMMANDS, n=1, cutoff=0.6)
        if matches:
            return f"Did you mean '{matches[0]}'?"

    # Fuzzy match against common options
    common_options = ["--help", "--debug", "--version", "--app", "--namespace"]
    matches = get_close_matches(user_input, common_options, n=1, cutoff=0.6)
    if matches:
        return f"Did you mean '{matches[0]}'?"

    return None


def format_error_with_suggestion(error_msg: str, user_input: str | None = None) -> str:
    """Format error message with optional suggestion.

    Args:
        error_msg: Main error message
        user_input: User input that caused the error (for suggestions)

    Returns:
        Formatted error with suggestion if available

    Examples:
        >>> msg = format_error_with_suggestion("Unknown command: startp", "startp")
        >>> print(msg)
        Unknown command: startp
        [hint] Did you mean 'startup'?
    """
    if user_input:
        suggestion = suggest_fix(user_input, context="command")
        if suggestion:
            return f"{error_msg}\n[hint]{suggestion}[/hint]"

    return error_msg
