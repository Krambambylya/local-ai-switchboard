"""Explicit response schemas for the health endpoints."""

from typing import Literal

from pydantic import BaseModel


class LiveStatus(BaseModel):
    """Process liveness. This body contains no configuration or secrets."""

    status: Literal["live"]


class ReadyStatus(BaseModel):
    """Readiness. This body is only ok or degraded."""

    status: Literal["ok", "degraded"]
