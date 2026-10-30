"""Explicit resource registration."""

from mcp.server import MCPServer

from gpt_parser_mcp.resources.info import register_info_resources
from gpt_parser_mcp.services.status import StatusService


def register_resources(server: MCPServer, status: StatusService) -> None:
    register_info_resources(server, status)
