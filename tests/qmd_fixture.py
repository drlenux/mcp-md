"""Create an executable QMD test double. Never used by production services."""
import sys
from pathlib import Path


def create_qmd_fixture(directory: Path) -> Path:
    command = directory / 'qmd-test-double'
    script = Path(__file__).parent / 'fixtures' / 'qmd_cli.py'
    command.write_text(f'#!{sys.executable}\n' + script.read_text())
    command.chmod(0o755)
    return command
