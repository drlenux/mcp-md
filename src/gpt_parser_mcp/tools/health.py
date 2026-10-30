from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from gpt_parser_mcp.services.status import ServerStatus, StatusService


def register_health_tools(server: MCPServer, status: StatusService) -> None:
    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def health_check() -> ServerStatus:
        """Check that this server process responds; return its name and version."""
        return status.get_status()
