"""
Source Code Environment Variable Scanners (AST & Regex)
"""

from __future__ import annotations

import ast
import logging
import re

logger = logging.getLogger(__name__)

# System/CI environment variables to exclude from the required contract
_SYSTEM_ENV_VARS: set[str] = {
    "PATH",
    "HOME",
    "USER",
    "SHELL",
    "PWD",
    "LANG",
    "TERM",
    "TMPDIR",
    "PYTHONPATH",
    "PYTHONDONTWRITEBYTECODE",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "NODE_ENV",
    "NODE_PATH",
    "DEBUG",
    "LOG_LEVEL",
    "PORT",
    "HOST",
    "CI",
    "CI_COMMIT_SHA",
    "GITHUB_ACTIONS",
    "RUNNER_OS",
}

_ENV_JS_DOT = re.compile(r"process\.env\.([A-Z_][A-Z0-9_]*)")
_ENV_JS_BRACKET = re.compile(r'process\.env\[[\'"]([\w]+)[\'"]\]')


def extract_env_keys_python(source: str, filepath: str = "<unknown>") -> set[str]:
    """
    Extract all env var keys referenced in Python source code via AST walking.
    """
    keys: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        logger.debug(f"[GitAgent] Syntax error in {filepath}, skipping AST scan")
        return keys

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            arg0 = node.args[0] if node.args else None

            # os.getenv("KEY")
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            ):
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    keys.add(arg0.value)

            # os.environ.get("KEY")
            elif (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"
            ):
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    keys.add(arg0.value)

            # getenv("KEY")
            elif isinstance(func, ast.Name) and func.id == "getenv":
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    keys.add(arg0.value)

        elif isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Attribute) and node.value.attr == "environ":
                slice_node = node.slice
                if isinstance(slice_node, ast.Constant) and isinstance(
                    slice_node.value, str
                ):
                    keys.add(slice_node.value)

    return keys


def extract_env_keys_js(source: str) -> set[str]:
    """
    Extract env var keys from JavaScript / TypeScript source.
    """
    keys: set[str] = set()
    keys.update(_ENV_JS_DOT.findall(source))
    keys.update(_ENV_JS_BRACKET.findall(source))
    return keys
