"""`litradar --help` is the first thing a newcomer reads: it must end with the "start here" epilog and list
every plugin command. The plugin loader in litradar.cli swallows import errors by design (a broken module
must not kill the CLI), so without this test a plugin that fails to import would silently vanish from
--help."""
import pytest

from litradar import cli


def _help_text(capsys) -> str:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    return capsys.readouterr().out


def test_help_ends_with_the_start_here_epilog(capsys):
    out = _help_text(capsys)
    assert "start here" in out
    for line in ("litradar check-config", "litradar run --days 7", "litradar status", "litradar serve"):
        assert line in out, line
    assert "exits 1 until the first run" in out
    assert "docs/FORKING.md" in out and "docs/deploy.md" in out
    assert ".env.example" in out
    # RawDescriptionHelpFormatter keeps the epilog's line breaks; the default formatter would reflow it into a blob
    assert out.rstrip().endswith("docs/COOKBOOK.md")


def test_help_lists_every_plugin_command(capsys):
    out = _help_text(capsys)
    for cmd in ("status", "serve", "update", "add", "track", "untrack", "coverage", "candidates", "concepts", "prioritize"):
        assert f"\n    {cmd} " in out, cmd
