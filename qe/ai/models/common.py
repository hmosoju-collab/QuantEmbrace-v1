"""Shared field types for qe.ai research records."""

from enum import StrEnum
from typing import Annotated

from pydantic import Field


class ComponentStatus(StrEnum):
    """Outcome of one research component. Only OK carries a score; every other
    status means "no AI input here" and fusion falls back to the quant path."""

    OK = "OK"
    UNAVAILABLE = "UNAVAILABLE"  # no data source / budget exhausted / breaker open
    MALFORMED = "MALFORMED"  # LLM output failed schema or evidence-id validation
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"
    BLOCKED = "BLOCKED"  # output contained a forbidden action; text dropped
    SKIPPED = "SKIPPED"  # not part of this research mode


Signed = Annotated[float, Field(ge=-1.0, le=1.0)]  # -1 bearish … +1 bullish
Unit = Annotated[float, Field(ge=0.0, le=1.0)]
ShortText = Annotated[str, Field(max_length=800)]
Point = Annotated[str, Field(max_length=240)]
