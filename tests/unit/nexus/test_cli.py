"""Unit tests for nexus.cli.main."""


def test_cli_has_server_group():
    from nexus.cli.main import cli

    assert cli is not None
    assert "server" in {c.name for c in cli.commands.values()}


def test_server_start_command_exists():
    from nexus.cli.main import cli

    server = cli.commands["server"]
    assert "start" in {c.name for c in server.commands.values()}


def test_approve_and_reject_commands_exist():
    """The HumanApprovalQueue docstring advertises `nexus approve <id>` /
    `nexus reject <id>` — both must exist as top-level commands."""
    from nexus.cli.main import cli

    names = {c.name for c in cli.commands.values()}
    assert "approve" in names
    assert "reject" in names


def test_approve_invokes_api_post(monkeypatch):
    """`nexus approve` POSTs to /approve/{id} on the configured API and prints
    the resulting status. We stub httpx.post so no network is touched."""
    import importlib

    # ``import nexus.cli.main as x`` binds to ``main`` the *function* (the
    # package __init__ re-exports it), so load the module explicitly.
    cli_main = importlib.import_module("nexus.cli.main")

    captured = {}

    class _FakeResp:
        status_code = 200
        _body = {"status": "approved", "action_id": "ABC12345",
                 "outcome": {"status": "success", "action_id": "audit-1"}}

        def json(self):
            return self._body

        def raise_for_status(self):
            pass

    def _fake_post(url, timeout=None):
        captured["url"] = url
        captured["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr(cli_main, "_RICH", False)
    # httpx is imported lazily inside _post_approval; patch the real httpx
    # module so the localized `import httpx` inside the fn picks up the stub.
    import httpx

    monkeypatch.setattr(httpx, "post", _fake_post)
    monkeypatch.setenv("NEXUS_API_URL", "http://api.example")

    from click.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(cli_main.cli, ["approve", "ABC12345", "--api-url", "http://api.example"])

    assert result.exit_code == 0, result.output
    assert captured["url"] == "http://api.example/approve/ABC12345"
    assert "approved" in result.output
