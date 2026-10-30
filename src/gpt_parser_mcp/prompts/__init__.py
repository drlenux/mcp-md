"""Explicit prompt registration."""

from mcp.server import MCPServer

from gpt_parser_mcp.prompts.diagnostics import register_diagnostic_prompts


def register_prompts(server: MCPServer) -> None:
    register_diagnostic_prompts(server)
