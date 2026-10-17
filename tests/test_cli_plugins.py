import subprocess
import sys


def test_cli_help_lists_core_commands():
    out = subprocess.run([sys.executable, "-m", "litradar", "--help"], capture_output=True, text=True, check=False)
    assert out.returncode == 0
    for cmd in ("run", "digest", "search", "rematch", "check-config"):
        assert cmd in out.stdout
