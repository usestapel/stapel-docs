"""Dataclass DTOs — the API models of stapel-docs (never ORM instances)."""
from dataclasses import dataclass


@dataclass
class PingResponse:
    """Response of the scaffold ping endpoint — replace with real DTOs."""

    greeting: str
