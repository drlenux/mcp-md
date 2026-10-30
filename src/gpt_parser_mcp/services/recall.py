"""QMD retrieval over small derived fragments, mapped back to current originals."""

import fcntl
import hashlib
import json
import math
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urlparse

from gpt_parser_mcp.services.errors import MarkdownError
from gpt_parser_mcp.services.line_index import file_version
from gpt_parser_mcp.services.markdown import MarkdownStore
from gpt_parser_mcp.services.models import RecallMatch, RecallResult


class QmdRunner:
    def __init__(self, command: str, cache_home: Path, timeout: float = 300, mode: str = "query"):
        self.command = command
        self.cache_home = cache_home.expanduser().resolve()
        self.timeout = timeout
        self.mode = mode

    def check(self) -> None:
        if shutil.which(self.command) is None:
            raise MarkdownError(
                "QMD is not installed. Install @tobilu/qmd and set MCP_QMD_COMMAND "
                "to its executable. recall requires QMD; other tools remain available."
            )

    def run(self, directory: Path, arguments: list[str], *, result: bool = False) -> object:
        environment = {
            **os.environ,
            "QMD_CONFIG_DIR": str(directory / "config"),
            "INDEX_PATH": str(directory / "qmd.sqlite"),
            "XDG_CACHE_HOME": str(self.cache_home),
            "PWD": str(directory),
            "NO_COLOR": "1",
        }
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            with subprocess.Popen(
                [self.command, "--index", "campaign", *arguments], cwd=directory,
                env=environment, stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                start_new_session=True,
            ) as process:
                try:
                    code = process.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise MarkdownError("QMD timed out; retry or increase MCP_QMD_TIMEOUT.") from None
            if code:
                errors.seek(max(0, errors.seek(0, os.SEEK_END) - 4096))
                raise MarkdownError(f"QMD failed ({code}): {errors.read().decode(errors='replace').strip()}")
            if not result:
                return None
            output.seek(0)
            raw = output.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise MarkdownError("QMD returned too much JSON; reduce limit.")
            # QMD 0.9 prints these instead of [] even with --json on empty results.
            if raw.strip() in {b"No results found.", b"No results found above minimum score threshold."}:
                return []
            try:
                return json.loads(raw)
            except ValueError:
                raise MarkdownError("QMD did not return valid JSON.") from None

    def refresh(self, directory: Path) -> None:
        self.run(directory, ["update"])
        if self.mode == "query":
            self.run(directory, ["embed"])

    def query(self, directory: Path, query: str, limit: int) -> object:
        return self.run(directory, [self.mode, "-c", "campaign", "-n", str(limit), "--json", "--", query], result=True)


class RecallService:
    def __init__(self, store: MarkdownStore, runner: QmdRunner, chunk_bytes: int = 8192):
        self.store = store
        self.runner = runner
        self.chunk_bytes = chunk_bytes

    def _fragments(self, source: BinaryIO) -> Iterator[tuple[bytes, int, int, int, int]]:
        buffer = bytearray()
        line_id = 1
        start_line = end_line = 1
        start_byte = position = 0
        while raw := source.readline(self.chunk_bytes):
            # Preserve UTF-8 boundaries even when one physical line exceeds the chunk size.
            for extra in range(4):
                try:
                    raw.decode("utf-8")
                    break
                except UnicodeDecodeError as exc:
                    if exc.reason != "unexpected end of data" or extra == 3:
                        raise
                    continuation = source.read(1)
                    if not continuation:
                        raise
                    raw += continuation
            if buffer and len(buffer) + len(raw) > self.chunk_bytes:
                yield bytes(buffer), start_line, end_line, start_byte, position
                buffer.clear()
            if not buffer:
                start_line, start_byte = line_id, position
            buffer.extend(raw)
            position += len(raw)
            end_line = line_id
            line_id += int(raw.endswith(b"\n"))
            if len(buffer) >= self.chunk_bytes or end_line - start_line >= 63:
                yield bytes(buffer), start_line, end_line, start_byte, position
                buffer.clear()
        if buffer:
            yield bytes(buffer), start_line, end_line, start_byte, position

    @staticmethod
    def _database(path: Path) -> sqlite3.Connection:
        if path.is_symlink():
            raise MarkdownError("Recall database must not be a symlink.")
        database = sqlite3.connect(path)
        database.row_factory = sqlite3.Row
        database.executescript("""
            CREATE TABLE IF NOT EXISTS sources (name TEXT PRIMARY KEY, version TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fragments (
                id TEXT PRIMARY KEY, table_name TEXT NOT NULL, version TEXT NOT NULL,
                start_line INTEGER, end_line INTEGER, start_byte INTEGER, end_byte INTEGER
            );
            CREATE INDEX IF NOT EXISTS fragments_table ON fragments(table_name);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
        """)
        return database

    def _sync(self, name: str, directory: Path, database: sqlite3.Connection) -> str:
        tables = self.store.list_tables(name)
        format_key = f"1:{self.chunk_bytes}"
        cached_format = database.execute("SELECT value FROM state WHERE key='format'").fetchone()
        if not cached_format or cached_format["value"] != format_key:
            with database:
                database.execute("DELETE FROM sources")
                database.execute("DELETE FROM fragments")
        chunks = directory / "chunks"
        chunks.mkdir(exist_ok=True)
        if chunks.is_symlink():
            raise MarkdownError("Recall chunks directory must not be a symlink.")
        versions = []
        for table in tables:
            with self.store._table(name, table) as (fd, filename), self.store._open(fd, filename) as source:
                version = file_version(source)
                versions.append((table, version))
                previous = database.execute("SELECT version FROM sources WHERE name=?", (table,)).fetchone()
                if previous and previous["version"] == version:
                    continue
                with database:
                    database.execute("DELETE FROM fragments WHERE table_name=?", (table,))
                    for data, first, last, begin, end in self._fragments(source):
                        identity = hashlib.sha256(f"{table}\0{version}\0{begin}\0{end}".encode()).hexdigest()
                        target = chunks / f"{identity}.md"
                        # Atomic publication also repairs leftovers from interrupted builds.
                        with tempfile.NamedTemporaryFile(dir=chunks, delete=False) as staged:
                            temporary = Path(staged.name)
                            try:
                                staged.write(data)
                                staged.flush()
                                os.replace(temporary, target)
                            finally:
                                temporary.unlink(missing_ok=True)
                        database.execute("INSERT INTO fragments VALUES (?,?,?,?,?,?,?)", (identity, table, version, first, last, begin, end))
                    if file_version(source) != version:
                        raise MarkdownError("File changed during recall indexing; retry.")
                    database.execute("INSERT OR REPLACE INTO sources VALUES (?,?)", (table, version))
        with database:
            for row in database.execute("SELECT name FROM sources").fetchall():
                if row["name"] not in tables:
                    database.execute("DELETE FROM fragments WHERE table_name=?", (row["name"],))
                    database.execute("DELETE FROM sources WHERE name=?", (row["name"],))
        # Remove obsolete derived documents before asking QMD to refresh its index.
        for path in chunks.iterdir():
            if path.suffix == ".md" and not database.execute("SELECT 1 FROM fragments WHERE id=?", (path.stem,)).fetchone():
                path.unlink()
        if self.store.list_tables(name) != tables:
            raise MarkdownError("Campaign files changed during indexing; retry recall.")
        with database:
            database.execute("INSERT OR REPLACE INTO state VALUES ('format',?)", (format_key,))
        return hashlib.sha256(json.dumps([self.chunk_bytes, versions]).encode()).hexdigest()

    def recall(self, name: str, query: str, limit: int) -> RecallResult:
        self.store._query(query)
        self.store._positive(limit)
        if not query.strip() or limit > min(100, self.store.max_result_lines):
            raise MarkdownError("recall requires nonempty q and limit between 1 and min(100, MCP_MAX_RESULT_LINES).")
        with self.store._campaign(name):
            pass
        self.runner.check()
        directory = self.store.base_dir / name / ".mcp-recall"
        directory.mkdir(exist_ok=True)
        if directory.is_symlink():
            raise MarkdownError("Recall cache must not be a symlink.")
        lock = os.open(directory / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise MarkdownError("Recall lock must be a regular file.")
            fcntl.flock(lock, fcntl.LOCK_EX)
            database = self._database(directory / "manifest.sqlite")
            try:
                signature = self._sync(name, directory, database)
                if not database.execute("SELECT 1 FROM fragments LIMIT 1").fetchone():
                    return RecallResult(matches=[], backend=f"qmd:{self.runner.mode}")
                config = directory / "config"
                config.mkdir(exist_ok=True)
                if config.is_symlink():
                    raise MarkdownError("QMD config directory must not be a symlink.")
                config_file = config / "campaign.yml"
                if config_file.is_symlink():
                    raise MarkdownError("QMD config must not be a symlink.")
                if (directory / "qmd.sqlite").is_symlink():
                    raise MarkdownError("QMD database must not be a symlink.")
                # JSON is valid YAML. Explicit index/config paths isolate each campaign.
                config_file.write_text(json.dumps({"collections": {"campaign": {"path": str(directory / "chunks"), "pattern": "*.md"}}}), encoding="utf-8")
                signature += ":" + self.runner.mode
                previous = database.execute("SELECT value FROM state WHERE key='synced'").fetchone()
                if not previous or previous["value"] != signature or not (directory / "qmd.sqlite").exists():
                    self.runner.refresh(directory)
                    with database:
                        database.execute("INSERT OR REPLACE INTO state VALUES ('synced',?)", (signature,))
                raw_results = self.runner.query(directory, query, limit)
                return self._results(name, database, raw_results, limit)
            finally:
                database.close()
        finally:
            os.close(lock)

    def _results(self, name: str, database: sqlite3.Connection, results: object, limit: int) -> RecallResult:
        if not isinstance(results, list):
            raise MarkdownError("Expected a JSON array of QMD results.")
        matches = []
        seen = set()
        used = 0
        truncated = False
        for item in results:
            if len(matches) >= limit:
                break
            try:
                uri = urlparse(item["file"])
                if uri.scheme != "qmd" or uri.netloc != "campaign":
                    raise ValueError()
                identity = Path(unquote(uri.path)).stem
                if uri.path != f"/{identity}.md":
                    raise ValueError()
                score = float(item["score"])
                if not math.isfinite(score):
                    raise ValueError()
            except (TypeError, KeyError, ValueError):
                raise MarkdownError("QMD returned an invalid or unscoped result.") from None
            if identity in seen:
                continue
            seen.add(identity)
            row = database.execute("SELECT * FROM fragments WHERE id=?", (identity,)).fetchone()
            if row is None:
                with database:
                    database.execute("DELETE FROM state WHERE key='synced'")
                raise MarkdownError("QMD returned an unknown fragment; retry recall to refresh the index.")
            with self.store._table(name, row["table_name"]) as (fd, filename), self.store._open(fd, filename) as source:
                if file_version(source) != row["version"]:
                    raise MarkdownError("Source changed since recall indexing; retry recall.")
                source.seek(row["start_byte"])
                raw = source.read(row["end_byte"] - row["start_byte"])
                if file_version(source) != row["version"]:
                    raise MarkdownError("Source changed during recall; retry.")
            if used + len(raw) > self.store.max_result_bytes:
                if not matches:
                    raise MarkdownError("Recall fragment exceeds MCP_MAX_RESULT_BYTES.")
                truncated = True
                break
            used += len(raw)
            matches.append(RecallMatch(
                table=row["table_name"], startLine=row["start_line"], endLine=row["end_line"],
                startByte=row["start_byte"], endByte=row["end_byte"], text=raw.decode("utf-8"),
                score=score, version=row["version"],
            ))
        return RecallResult(matches=matches, backend=f"qmd:{self.runner.mode}", truncated=truncated)
