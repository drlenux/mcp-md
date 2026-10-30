import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from gpt_parser_mcp import __version__
from gpt_parser_mcp.config import Settings, load_settings


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_configuration_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / '.env'
            env_file.write_text('MCP_PORT=9000\nMCP_SERVER_NAME=from-file\n', encoding='utf-8')
            os.environ['MCP_PORT'] = '9001'
            settings = Settings(_env_file=env_file)
            self.assertEqual(settings.port, 9001)
            self.assertEqual(settings.server_name, 'from-file')
            self.assertEqual(Settings(_env_file=env_file, port=9002).port, 9002)

    def test_invalid_configuration_is_rejected(self) -> None:
        for values in (
            {'port': 0},
            {'port': 65536},
            {'transport': 'sse'},
            {'log_level': 'invalid'},
            {'server_name': '  '},
            {'host': ''},
            {'base_dir': ''},
            {'max_result_bytes': 0},
            {'max_result_lines': 0},
            {'qmd_timeout': 0},
            {'qmd_mode': 'unknown'},
            {'qmd_command': ''},
            {'recall_chunk_bytes': 128},
            {'recall_chunk_bytes': 65537},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                Settings(**values)

    def test_default_dotenv_is_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, '.env').write_text('MCP_PORT=9000\nBASE_DIR=./campaigns\n', encoding='utf-8')
            result = subprocess.run(
                [sys.executable, '-c', 'from gpt_parser_mcp.config import load_settings; print(load_settings().port); print(load_settings().base_dir)'],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            self.assertEqual(result.stdout.splitlines(), ['9000', str(Path(directory, 'campaigns').resolve())])

    def test_base_dir_is_relative_to_explicit_env_file_and_environment_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / '.env'
            env_file.write_text('BASE_DIR=./files\n', encoding='utf-8')
            self.assertEqual(load_settings(env_file).base_dir, (Path(directory) / 'files').resolve())
            os.environ['BASE_DIR'] = './override'
            self.assertEqual(load_settings(env_file).base_dir, (Path(directory) / 'override').resolve())

    def test_qmd_paths_are_relative_to_environment_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / '.env'
            env_file.write_text('MCP_QMD_COMMAND=./bin/qmd\nMCP_QMD_CACHE_HOME=./models\nMCP_QMD_MODE=search\n')
            settings = load_settings(env_file)
            self.assertEqual(settings.qmd_command, str((Path(directory) / 'bin/qmd').resolve()))
            self.assertEqual(settings.qmd_cache_home, (Path(directory) / 'models').resolve())
            self.assertEqual(settings.qmd_mode, 'search')


class CliTests(unittest.TestCase):
    def test_installed_entrypoint_from_another_directory(self) -> None:
        command = Path(sys.executable).parent / 'gpt-parser-mcp'
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [str(command), '--version'],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
        self.assertEqual(result.stdout.strip(), __version__)

    def test_invalid_config_exits_before_starting_protocol(self) -> None:
        for arguments in (['--port', '0'], ['--env-file', '/nonexistent/mcp.env']):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, '-m', 'gpt_parser_mcp', *arguments],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn('error:', result.stderr)
                self.assertNotIn('Traceback', result.stderr)
