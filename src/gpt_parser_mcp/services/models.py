from pydantic import BaseModel


class LineMatch(BaseModel):
    lineId: int
    text: str


class SearchPage(BaseModel):
    matches: list[LineMatch]
    nextCursor: str | None
    version: str


class RecallMatch(BaseModel):
    table: str
    startLine: int
    endLine: int
    startByte: int
    endByte: int
    text: str
    score: float
    version: str


class RecallResult(BaseModel):
    matches: list[RecallMatch]
    backend: str = "qmd"
    truncated: bool = False
