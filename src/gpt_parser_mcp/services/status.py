"""Process liveness and public server metadata."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel


class ServerStatus(BaseModel):
    status: Literal["ok"] = "ok"
    name: str
    version: str


@dataclass(frozen=True)
class StatusService:
    name: str
    version: str

    def get_status(self) -> ServerStatus:
        """Report process liveness, without implying downstream readiness."""
        return ServerStatus(name=self.name, version=self.version)
