"""Composition root: build services and explicitly register MCP handlers."""

from mcp.server import MCPServer

from gpt_parser_mcp import __version__
from gpt_parser_mcp.config import Settings, load_settings
from gpt_parser_mcp.prompts import register_prompts
from gpt_parser_mcp.resources import register_resources
from gpt_parser_mcp.services.status import StatusService
from gpt_parser_mcp.services.markdown import MarkdownStore
from gpt_parser_mcp.services.recall import QmdRunner, RecallService
from gpt_parser_mcp.tools import register_tools


def create_server(settings: Settings | None = None) -> MCPServer:
    config = settings if settings is not None else load_settings()
    status = StatusService(name=config.server_name, version=__version__)
    store = MarkdownStore(
        config.base_dir,
        max_result_bytes=config.max_result_bytes,
        max_result_lines=config.max_result_lines,
    )
    recall = RecallService(
        store,
        QmdRunner(config.qmd_command, config.qmd_cache_home, config.qmd_timeout, config.qmd_mode),
        config.recall_chunk_bytes,
    )
    server = MCPServer(
        config.server_name,
        version=__version__,
        instructions=(
            "Manage campaign Markdown files with new, list, listTables, get, insert, search, searchPage, update and getAll. "
            "name identifies a campaign; table is a filename without .md. Line IDs start at 1. "
            "For large files prefer get or searchPage. searchPage returns line IDs and a version-bound cursor. "
            "Use recall for ranked QMD retrieval within one campaign; it requires QMD and refreshes derived indexes. "
            "Use health_check for process liveness and server://info for server metadata."
        ),
        log_level=config.log_level,
    )
    register_tools(server, status, store, recall)
    register_resources(server, status)
    register_prompts(server)
    return server
