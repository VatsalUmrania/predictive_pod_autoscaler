"""Unit tests for nexus.cli.main."""


def test_cli_has_server_group():
    from nexus.cli.main import cli

    assert cli is not None
    assert "server" in {c.name for c in cli.commands.values()}


def test_server_start_command_exists():
    from nexus.cli.main import cli

    server = cli.commands["server"]
    assert "start" in {c.name for c in server.commands.values()}
