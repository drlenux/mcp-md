"""Explicit tool registration."""

from mcp.server import MCPServer

from gpt_parser_mcp.services.status import StatusService
from gpt_parser_mcp.services.markdown import MarkdownStore
from gpt_parser_mcp.services.recall import RecallService
from gpt_parser_mcp.tools.health import register_health_tools
from gpt_parser_mcp.tools.markdown import register_markdown_tools


def register_tools(server: MCPServer, status: StatusService, store: MarkdownStore, recall: RecallService) -> None:
    register_health_tools(server, status)
    register_markdown_tools(server, store, recall)
