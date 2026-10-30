"""Line-oriented Markdown storage with bounded I/O and process-safe writes."""

from __future__ import annotations

import fcntl
import base64
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from gpt_parser_mcp.services.errors import MarkdownError
from gpt_parser_mcp.services.line_index import LineIndex, file_version
from gpt_parser_mcp.services.models import LineMatch, SearchPage

CHUNK_SIZE = 1024 * 1024


class MarkdownStore:
    def __init__(self, base_dir: Path, *, max_result_bytes: int = 1024 * 1024, max_result_lines: int = 10_000):
        if max_result_bytes < 1 or max_result_lines < 1:
            raise ValueError("Result limits must be positive.")
        self.base_dir = base_dir.expanduser().resolve()
        self.max_result_bytes = max_result_bytes
        self.max_result_lines = max_result_lines
        self.line_index = LineIndex()

    @staticmethod
    def _component(value: str) -> str:
        if not value.strip() or value in {".", ".."} or any(c in value for c in "/\\\x00\r\n"):
            raise MarkdownError("name and table must be nonempty single path components.")
        return value

    @staticmethod
    def _line(value: str) -> bytes:
        if any(c in value for c in "\r\n\x00"):
            raise MarkdownError("line must contain exactly one line, without CR, LF or NUL.")
        return value.encode("utf-8")

    @staticmethod
    def _positive(value: int) -> None:
        if type(value) is not int or value < 1:
            raise MarkdownError("Line IDs start at 1 and must be positive integers.")

    @contextmanager
    def _campaign(self, name: str) -> Iterator[int]:
        path = self.base_dir / self._component(name)
        # The directory descriptor also anchors subsequent opens during renames.
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def _table(self, name: str, table: str, *, write: bool = False) -> Iterator[tuple[int, str]]:
        filename = self._component(table) + ".md"
        with self._campaign(name) as directory:
            # A stable separate inode is necessary because update replaces the data inode.
            lock = os.open(
                f".{filename}.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
            try:
                if not stat.S_ISREG(os.fstat(lock).st_mode):
                    raise MarkdownError("The lock must be a regular file.")
                fcntl.flock(lock, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
                yield directory, filename
            finally:
                os.close(lock)

    @staticmethod
    def _open(directory: int, filename: str, *, append: bool = False) -> BinaryIO:
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT if append else os.O_RDONLY
        descriptor = os.open(
            filename, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644, dir_fd=directory
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise MarkdownError("The table must be a regular Markdown file.")
            return os.fdopen(descriptor, "a+b" if append else "rb")
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _decode(raw: bytes) -> str:
        if raw.endswith(b"\n"):
            raw = raw[:-1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
        return raw.decode("utf-8")

    def _read_result(self, source: BinaryIO, count: int | None = None) -> list[str]:
        result = []
        size = 0
        while count is None or len(result) < count:
            # A bounded readline also rejects an oversized individual line early.
            raw = source.readline(self.max_result_bytes - size + 1)
            if not raw:
                return result
            size += len(raw)
            if size > self.max_result_bytes or len(result) >= self.max_result_lines:
                raise MarkdownError(
                    "Result exceeds MCP_MAX_RESULT_BYTES or MCP_MAX_RESULT_LINES; "
                    "no partial result returned. Use get with a smaller countLine, "
                    "narrow the search, or increase the configured limits for an oversized line."
                )
            result.append(self._decode(raw))
        return result

    def new(self, name: str) -> None:
        path = self.base_dir / self._component(name)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        path.mkdir(exist_ok=True)
        with self._campaign(name):
            pass

    def list(self) -> list[str]:
        if not self.base_dir.exists():
            return []
        with os.scandir(self.base_dir) as entries:
            return sorted(entry.name for entry in entries if entry.is_dir(follow_symlinks=False))

    def list_tables(self, name: str) -> list[str]:
        with self._campaign(name) as directory, os.scandir(directory) as entries:
            tables = []
            for entry in entries:
                if entry.name.endswith(".md") and entry.is_file(follow_symlinks=False):
                    try:
                        tables.append(self._component(entry.name[:-3]))
                    except MarkdownError:
                        continue
            return sorted(tables)

    def get(self, name: str, table: str, line_id: int, count_line: int) -> list[str]:
        self._positive(line_id)
        if type(count_line) is not int or count_line < 0:
            raise MarkdownError("countLine must be a nonnegative integer.")
        with self._table(name, table) as (directory, filename):
            with self._open(directory, filename) as source:
                if count_line == 0:
                    return []
                self.line_index.seek(source, directory, filename, line_id)
                return self._read_result(source, count_line)

    @staticmethod
    def _query(query: str) -> None:
        if any(c in query for c in "\x00\n\r"):
            raise MarkdownError("q must be a single-line string without NUL.")

    def search_page(self, name: str, table: str, query: str, limit: int, cursor: str | None = None) -> SearchPage:
        self._query(query)
        self._positive(limit)
        if limit > self.max_result_lines:
            raise MarkdownError("limit exceeds MCP_MAX_RESULT_LINES.")
        scope = hashlib.sha256(json.dumps([str(self.base_dir), name, table, query]).encode()).hexdigest()
        with self._table(name, table) as (directory, filename), self._open(directory, filename) as source:
            version = file_version(source)
            start = 1
            if cursor is not None:
                try:
                    if len(cursor) > 2048:
                        raise ValueError()
                    data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                    if data["format"] != 1 or data["scope"] != scope:
                        raise ValueError()
                    start = data["line"]
                    self._positive(start)
                    cursor_version = data["version"]
                except (ValueError, KeyError, TypeError):
                    raise MarkdownError("Invalid cursor or cursor belongs to another search.") from None
                if cursor_version != version:
                    raise MarkdownError("File changed; restart searchPage without a cursor.")
            self.line_index.seek(source, directory, filename, start)
            # grep inherits the descriptor, not Python's read-ahead buffer.
            os.lseek(source.fileno(), source.tell(), os.SEEK_SET)
            matches = []
            used = 0
            more = False
            with tempfile.TemporaryFile() as errors:
                with subprocess.Popen(
                    ["grep", "-a", "-n", "-F", "-m", str(limit + 1), "-e", query],
                    stdin=source, stdout=subprocess.PIPE, stderr=errors,
                    env={**os.environ, "LC_ALL": "C"},
                ) as process:
                    try:
                        assert process.stdout is not None
                        while raw := process.stdout.readline(self.max_result_bytes + 64):
                            prefix, separator, body = raw.partition(b":")
                            if not separator or not prefix.isdigit():
                                raise MarkdownError("Unexpected output from grep.")
                            if len(matches) >= limit or used + len(body) > self.max_result_bytes:
                                if not matches:
                                    raise MarkdownError("Matching line exceeds MCP_MAX_RESULT_BYTES.")
                                more = True
                                process.kill()
                                break
                            matches.append(LineMatch(lineId=start + int(prefix) - 1, text=self._decode(body)))
                            used += len(body)
                        code = process.wait()
                    except BaseException:
                        process.kill()
                        raise
                if not more and code not in (0, 1):
                    errors.seek(0)
                    raise MarkdownError(f"grep failed: {errors.read(4096).decode(errors='replace')}")
            if file_version(source) != version:
                raise MarkdownError("File changed during search; retry without a cursor.")
            next_cursor = None
            if more:
                data = {"format": 1, "scope": scope, "version": version, "line": matches[-1].lineId + 1}
                next_cursor = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
            return SearchPage(matches=matches, nextCursor=next_cursor, version=version)

    def insert(self, name: str, table: str, line: str) -> None:
        payload = self._line(line)
        with self._table(name, table, write=True) as (directory, filename):
            with self._open(directory, filename, append=True) as destination:
                destination.seek(0, os.SEEK_END)
                if destination.tell():
                    destination.seek(-1, os.SEEK_END)
                    if destination.read(1) != b"\n":
                        payload = b"\n" + payload
                destination.write(payload + b"\n")
                destination.flush()
                os.fsync(destination.fileno())

    def search(self, name: str, table: str, query: str) -> list[str]:
        self._query(query)
        with self._table(name, table) as (directory, filename):
            with self._open(directory, filename) as source:
                # -F is literal matching; -e prevents a query from becoming a flag.
                # Stream stdout directly into the result, without a second full copy.
                with tempfile.TemporaryFile() as errors:
                    with subprocess.Popen(
                        ["grep", "-a", "-F", "-e", query],
                        stdin=source,
                        stdout=subprocess.PIPE,
                        stderr=errors,
                        env={**os.environ, "LC_ALL": "C"},
                    ) as process:
                        try:
                            assert process.stdout is not None
                            result = self._read_result(process.stdout)
                            returncode = process.wait()
                        except BaseException:
                            process.kill()
                            raise
                    if returncode not in (0, 1):
                        errors.seek(0)
                        detail = errors.read(4096).decode("utf-8", errors="replace").strip()
                        raise MarkdownError(f"grep failed with exit code {returncode}: {detail}")
                    return result

    def get_all(self, name: str, table: str) -> list[str]:
        with self._table(name, table) as (directory, filename):
            with self._open(directory, filename) as source:
                return self._read_result(source)

    def update(self, name: str, table: str, line_ids: list[int], line: str) -> None:
        replacement = self._line(line)
        for line_id in line_ids:
            self._positive(line_id)
        targets = set(line_ids)
        with self._table(name, table, write=True) as (directory, filename):
            with self._open(directory, filename) as source:
                if not targets:
                    return
                # Create the temporary file relative to the same anchored directory.
                temporary = f".mcp-{secrets.token_hex(16)}.tmp"
                descriptor = os.open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory
                )
                try:
                    with os.fdopen(descriptor, "wb") as destination:
                        self._rewrite(source, destination, targets, replacement)
                        destination.flush()
                        os.fchmod(destination.fileno(), stat.S_IMODE(os.fstat(source.fileno()).st_mode))
                        os.fsync(destination.fileno())
                    os.replace(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass

    @staticmethod
    def _rewrite(source: BinaryIO, destination: BinaryIO, targets: set[int], replacement: bytes) -> None:
        last_target = max(targets)
        line_id = 1
        previous_byte = b""
        partial = False
        while fragment := source.readline(CHUNK_SIZE):
            partial = True
            complete = fragment.endswith(b"\n")
            if line_id not in targets:
                destination.write(fragment)
            elif complete:
                crlf = fragment.endswith(b"\r\n") or (fragment == b"\n" and previous_byte == b"\r")
                destination.write(replacement + (b"\r\n" if crlf else b"\n"))
            previous_byte = fragment[-1:]
            if complete:
                if line_id == last_target:
                    shutil.copyfileobj(source, destination, CHUNK_SIZE)
                    return
                line_id += 1
                partial = False
                previous_byte = b""
        if partial and line_id == last_target:
            destination.write(replacement)
            return
        raise MarkdownError(f"Line ID {last_target} is beyond the end of the file; nothing was updated.")
