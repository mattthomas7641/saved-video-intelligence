"""Pydantic response models for the JSON routes (just /health now: the daily-agent
API this file used to describe was retired with the move to Sidequest)."""
from pydantic import BaseModel


class HealthResponse(BaseModel):
    ok: bool = True
