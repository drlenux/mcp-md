"""MCP API for campaign Markdown files."""

from collections.abc import Iterator
from contextlib import contextmanager
import sqlite3
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from gpt_parser_mcp.services.markdown import MarkdownError, MarkdownStore
from gpt_parser_mcp.services.models import RecallResult, SearchPage
from gpt_parser_mcp.services.recall import RecallService

LineId = Annotated[int, Field(strict=True, ge=1)]
LineCount = Annotated[int, Field(strict=True, ge=0)]


@contextmanager
def storage_errors() -> Iterator[None]:
    try:
        yield
    except (MarkdownError, OSError, UnicodeError, sqlite3.Error) as exc:
        raise ToolError(str(exc)) from exc


def register_markdown_tools(server: MCPServer, store: MarkdownStore, recall_service: RecallService) -> None:
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    create = ToolAnnotations(destructive_hint=False, idempotent_hint=True, open_world_hint=False)
    append = ToolAnnotations(destructive_hint=False, idempotent_hint=False, open_world_hint=False)
    replace = ToolAnnotations(destructive_hint=True, idempotent_hint=True, open_world_hint=False)

    @server.tool(annotations=create)
    def new(name: str) -> None:
        """Create a campaign directory. An existing campaign is left intact."""
        with storage_errors():
            store.new(name)

    @server.tool(name="list", annotations=read_only)
    def list_campaigns() -> list[str]:
        """List campaign names in alphabetical order."""
        with storage_errors():
            return store.list()

    @server.tool(name="listTables", annotations=read_only)
    def list_tables(name: str) -> list[str]:
        """List direct Markdown filenames without .md in this campaign, alphabetically."""
        with storage_errors():
            return store.list_tables(name)

    @server.tool(name="searchPage", annotations=read_only)
    def search_page(name: str, table: str, q: str, limit: LineId, cursor: str | None = None) -> SearchPage:
        """Search via literal case-sensitive grep, returning line IDs, text and file version.

        Reuse nextCursor with the same name/table/q. null means end of results.
        A changed file invalidates the cursor. Pages may be shorter than limit to
        respect the byte budget; follow nextCursor rather than relying on length.
        """
        with storage_errors():
            return store.search_page(name, table, q, limit, cursor)

    @server.tool(annotations=read_only)
    def recall(name: str, q: str, limit: Annotated[int, Field(strict=True, ge=1, le=100)]) -> RecallResult:
        """Retrieve relevant passages from this campaign with QMD (required).

        Refreshes changed derived fragments before searching. Returns original table,
        1-based line range, 0-based UTF-8 byte offsets (end exclusive), text, score and
        source version. Semantic query mode is the default; backend identifies the mode.
        Re-read and check the current source before editing a retrieved passage.
        """
        with storage_errors():
            return recall_service.recall(name, q, limit)

    @server.tool(annotations=read_only)
    def get(name: str, table: str, lineId: LineId, countLine: LineCount) -> list[str]:
        """Read up to countLine lines starting at lineId (1-based). table excludes .md.

        Returns [] past EOF. Prefer small pages for large files. Oversized results
        fail explicitly according to MCP_MAX_RESULT_BYTES / MCP_MAX_RESULT_LINES.
        """
        with storage_errors():
            return store.get(name, table, lineId, countLine)

    @server.tool(annotations=append)
    def insert(name: str, table: str, line: str) -> None:
        """Append one line, creating {table}.md if absent. The campaign must exist.

        line must not contain CR, LF or NUL; an empty string appends a blank line.
        """
        with storage_errors():
            store.insert(name, table, line)

    @server.tool(annotations=read_only)
    def search(name: str, table: str, q: str) -> list[str]:
        """Find all lines containing q via grep -F (literal, case-sensitive).

        Results retain file order, including duplicates. An empty query matches all
        lines. Oversized results fail explicitly, without truncation, according to
        MCP_MAX_RESULT_BYTES / MCP_MAX_RESULT_LINES. Use searchPage to paginate matches.
        """
        with storage_errors():
            return store.search(name, table, q)

    @server.tool(annotations=replace)
    def update(name: str, table: str, lineId: list[LineId], line: str) -> None:
        """Replace each specified 1-based line with the same single-line string.

        Duplicate IDs are ignored; [] is a no-op. Invalid IDs leave the file unchanged.
        Streams to a temporary file and atomically replaces the original.
        """
        with storage_errors():
            store.update(name, table, lineId, line)

    @server.tool(name="getAll", annotations=read_only)
    def get_all(name: str, table: str) -> list[str]:
        """Read all lines of {table}.md, without line terminators.

        Oversized results fail explicitly according to MCP_MAX_RESULT_BYTES /
        MCP_MAX_RESULT_LINES. Use get with a small countLine for large files.
        """
        with storage_errors():
            return store.get_all(name, table)
