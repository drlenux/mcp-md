from mcp.server import MCPServer


def register_diagnostic_prompts(server: MCPServer) -> None:
    @server.prompt()
    def diagnose_server() -> str:
        """Verify the MCP connection and report the server identity."""
        return (
            "Call health_check and read server://info. Report whether both requests "
            "succeeded and whether the server name and version agree. "
            "This checks process liveness only, not external dependencies."
        )
