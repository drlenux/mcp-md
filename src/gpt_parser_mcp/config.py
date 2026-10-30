"""Validated configuration; loading it never starts the server."""

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Transport = Literal["stdio", "streamable-http"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MCP_",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        str_strip_whitespace=True,
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    server_name: str = Field(default="gpt-parser-mcp", min_length=1)
    transport: Transport = "stdio"
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: LogLevel = "INFO"
    base_dir: Path = Field(
        default=Path("data"), validation_alias=AliasChoices("BASE_DIR", "MCP_BASE_DIR")
    )
    max_result_bytes: int = Field(default=1024 * 1024, ge=1)
    max_result_lines: int = Field(default=10_000, ge=1)
    qmd_command: str = Field(default="qmd", min_length=1)
    qmd_cache_home: Path = Path(".cache")
    qmd_timeout: float = Field(default=300, gt=0, le=3600)
    qmd_mode: Literal["query", "search"] = "query"
    recall_chunk_bytes: int = Field(default=8192, ge=256, le=65536)

    @field_validator("base_dir", mode="before")
    @classmethod
    def validate_base_dir(cls, value: object) -> object:
        if isinstance(value, str) and (not value.strip() or "\x00" in value):
            raise ValueError("BASE_DIR must be a nonempty filesystem path.")
        return value


def load_settings(env_file: Path | None = None, **overrides: object) -> Settings:
    """Load .env and anchor relative storage paths to the configuration directory."""
    selected = (env_file or Path.cwd() / ".env").expanduser().absolute()
    if env_file is not None and not selected.is_file():
        raise FileNotFoundError(f"Environment file does not exist: {selected}")
    settings = Settings(_env_file=selected, **overrides)
    base_dir = settings.base_dir.expanduser()
    if not base_dir.is_absolute():
        base_dir = selected.parent / base_dir
    cache_home = settings.qmd_cache_home.expanduser()
    if not cache_home.is_absolute():
        cache_home = selected.parent / cache_home
    command = settings.qmd_command
    if "/" in command:
        executable = Path(command).expanduser()
        if not executable.is_absolute():
            executable = selected.parent / executable
        command = str(executable.resolve())
    return settings.model_copy(update={
        "base_dir": base_dir.resolve(), "qmd_cache_home": cache_home.resolve(), "qmd_command": command,
    })
