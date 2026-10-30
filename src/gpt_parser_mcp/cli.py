"""Command-line entry point and transport selection."""

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from gpt_parser_mcp import __version__
from gpt_parser_mcp.config import load_settings
from gpt_parser_mcp.server import create_server


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the gpt-parser MCP server.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--transport", choices=("stdio", "streamable-http"))
    parser.add_argument("--host", help="HTTP bind address (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, help="HTTP port (default: 8000).")
    parser.add_argument("--env-file", type=Path, help="Dotenv path (default: .env in the working directory).")
    args = parser.parse_args(argv)

    if args.env_file is not None and not args.env_file.is_file():
        parser.error(f"Environment file does not exist: {args.env_file}")

    overrides = {
        key: value
        for key, value in vars(args).items()
        if key != "env_file" and value is not None
    }
    try:
        settings = load_settings(args.env_file, **overrides)
    except (ValidationError, OSError) as exc:
        parser.error(str(exc))

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    server = create_server(settings)
    logging.getLogger(__name__).info("Starting %s via %s", settings.server_name, settings.transport)
    try:
        if settings.transport == "stdio":
            server.run(transport="stdio")
        else:
            server.run(
                transport="streamable-http",
                host=settings.host,
                port=settings.port,
                stateless_http=True,
                json_response=True,
            )
    except KeyboardInterrupt:
        pass
