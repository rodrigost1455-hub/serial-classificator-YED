"""Pydantic response models for the JSON endpoints."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class IngestResult(BaseModel):
    added: int
    skipped: int
    quarantined: int
    files: int


class CohortRollup(BaseModel):
    cohort: str
    total: int
    fordRisk: int
    testerNG: int


class Summary(BaseModel):
    total: int
    fordPass: int
    fordRisk: int
    testerNG: int
    overReject: int
    anomalies: int
    meanS: Optional[float]
    fordLimitS: float
    byCohort: List[CohortRollup]


class UnitsPage(BaseModel):
    total: int          # rows matching the filter
    limit: int
    offset: int
    rows: List[dict]


class CrossRefHit(BaseModel):
    SerialNumber: str
    Ship_Date: Optional[str] = None
    Destination_Plant: Optional[str] = None
    Ford_220A: str
    Status: str
    S_High_mVA: Optional[float] = None
    Proy_220A_High_mV: Optional[float] = None


class CrossRefResult(BaseModel):
    shipped_rows: int
    matched: int
    at_risk: int
    hits: List[CrossRefHit]
