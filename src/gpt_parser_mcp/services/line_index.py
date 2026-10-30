"""Persistent sparse byte offsets; bounded memory even for enormous lines."""

import hashlib
import json
import os
import secrets
import stat
import struct
from typing import BinaryIO

from gpt_parser_mcp.services.errors import MarkdownError

HEADER_SIZE = 512
OFFSET = struct.Struct("<Q")
BLOCK_SIZE = 1024 * 1024


def file_version(source: BinaryIO) -> str:
    info = os.fstat(source.fileno())
    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return hashlib.sha256(repr(identity).encode()).hexdigest()


class LineIndex:
    def __init__(self, stride: int = 256):
        self.stride = stride

    def _open(self, directory: int, filename: str, version: str) -> BinaryIO | None:
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None
        stream = os.fdopen(fd, "rb")
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise MarkdownError("Line index must be a regular file.")
            header = json.loads(stream.read(HEADER_SIZE).rstrip(b" \n"))
            if (header.get("format") != 1 or header.get("version") != version
                    or header.get("stride") != self.stride):
                stream.close()
                return None
            count = header["lines"]
            expected = HEADER_SIZE + max(1, (count + self.stride - 1) // self.stride) * OFFSET.size
            if type(count) is not int or count < 0 or os.fstat(fd).st_size != expected:
                stream.close()
                return None
            return stream
        except (ValueError, TypeError, KeyError, AttributeError):
            stream.close()
            return None
        except BaseException:
            stream.close()
            raise

    def _build(self, source: BinaryIO, directory: int, filename: str, version: str) -> None:
        temporary = f".mcp-lines-{secrets.token_hex(12)}.tmp"
        fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "w+b") as index:
                index.write(b" " * HEADER_SIZE)
                index.write(OFFSET.pack(0))
                source.seek(0)
                offset = 0
                newlines = 0
                last = b""
                size = os.fstat(source.fileno()).st_size
                while block := source.read(BLOCK_SIZE):
                    pos = 0
                    while (pos := block.find(b"\n", pos)) != -1:
                        newlines += 1
                        pos += 1
                        if newlines % self.stride == 0 and offset + pos < size:
                            index.write(OFFSET.pack(offset + pos))
                    offset += len(block)
                    last = block[-1:]
                if file_version(source) != version:
                    raise MarkdownError("File changed while building the line index; retry.")
                count = newlines + int(bool(last) and last != b"\n")
                header = json.dumps({"format": 1, "version": version, "stride": self.stride, "lines": count}).encode()
                index.seek(0)
                index.write(header.ljust(HEADER_SIZE, b" "))
                index.flush()
                os.fsync(index.fileno())
            os.replace(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    def seek(self, source: BinaryIO, directory: int, filename: str, line_id: int) -> str:
        version = file_version(source)
        source.seek(0)
        remaining = line_id - 1
        if line_id > self.stride:
            index_name = f".{filename}.lines"
            index = self._open(directory, index_name, version)
            if index is None:
                self._build(source, directory, index_name, version)
                index = self._open(directory, index_name, version)
            if index is None:
                raise MarkdownError("Line index changed while opening it; retry.")
            with index:
                index.seek(0)
                header = json.loads(index.read(HEADER_SIZE))
                if line_id > header["lines"]:
                    source.seek(0, os.SEEK_END)
                    return version
                checkpoint = (line_id - 1) // self.stride
                index.seek(HEADER_SIZE + checkpoint * OFFSET.size)
                offset = OFFSET.unpack(index.read(OFFSET.size))[0]
                if offset > os.fstat(source.fileno()).st_size:
                    raise MarkdownError("Invalid byte offset in line index; remove the .lines cache.")
                source.seek(offset)
                remaining -= checkpoint * self.stride
        while remaining:
            fragment = source.readline(BLOCK_SIZE)
            if not fragment:
                break
            if fragment.endswith(b"\n"):
                remaining -= 1
        return version
