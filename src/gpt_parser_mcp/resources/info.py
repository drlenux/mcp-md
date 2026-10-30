from mcp.server import MCPServer

from gpt_parser_mcp.services.status import StatusService


def register_info_resources(server: MCPServer, status: StatusService) -> None:
    @server.resource("server://info", mime_type="application/json")
    def server_info() -> str:
        """Read public server metadata and process liveness."""
        return status.get_status().model_dump_json()
