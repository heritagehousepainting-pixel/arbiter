from typer.testing import CliRunner

from arbiter.cli import app


def test_monday_refresh_command_registered():
    res = CliRunner().invoke(app, ["monday-refresh", "--help"])
    assert res.exit_code == 0
    assert "Monday" in res.output or "refresh" in res.output.lower()
