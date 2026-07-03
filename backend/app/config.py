"""Central configuration — the single source of truth for limits and rules.

Quality Engineering tunes these values; nothing below should be duplicated as a
literal anywhere else in the codebase. Every threshold can be overridden with an
environment variable so the running service can be retuned without a code change.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Dict


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw.replace(",", "."))


@dataclass(frozen=True)
class Limits:
    # --- our EOL (tester) acceptance rule -----------------------------------
    TESTER_MIN_S: float = _env_float("TESTER_MIN_S", 5.700)      # mV/A, High range
    TESTER_MIN_S_LOW: float = _env_float("TESTER_MIN_S_LOW", 19.700)  # mV/A, Low range
    RATIO_MIN: float = _env_float("RATIO_MIN", 0.280)           # min High/Low slope ratio
    # --- Ford's field limit --------------------------------------------------
    FORD_LIMIT_V: float = _env_float("FORD_LIMIT_V", 3.746)      # V at 220 A, lower limit
    # --- physics constants ---------------------------------------------------
    NOMINAL_RATIO: float = _env_float("NOMINAL_RATIO", 0.2885)   # nominal S_High/S_Low
    NOMINAL_OFFSET_V: float = _env_float("NOMINAL_OFFSET_V", 2.505)  # V at 0 A (for fordLimitS)
    ANOMALY_S_MAX: float = _env_float("ANOMALY_S_MAX", 1.000)    # S_High below this = dead/inverted
    TEST_CURRENT_A: float = _env_float("TEST_CURRENT_A", 0.020)  # the 20 mA step used at EOL
    PROJECTION_CURRENT_A: float = _env_float("PROJECTION_CURRENT_A", 220.0)  # Ford's operating point

    @property
    def ford_limit_s_mVA(self) -> float:
        """Equivalent High-range slope that lands exactly on Ford's limit at the
        nominal offset. The dashboard draws this as the 'Ford line'."""
        return (self.FORD_LIMIT_V - self.NOMINAL_OFFSET_V) * 1000.0 / self.PROJECTION_CURRENT_A


# Valid cohort tags. The ingest job assigns one; anything else is rejected so a
# typo in a folder name can't silently create a phantom cohort.
VALID_COHORTS = ("CORRECTION_FACTOR", "PRODUCCION_SOSPECHOSA", "SDACS")
DEFAULT_COHORT = "CORRECTION_FACTOR"


@dataclass(frozen=True)
class Precision:
    """Decimal places for CSV emission (§3 of the spec)."""
    voltage: int = 4        # offsets / 20 A voltages
    delta: int = 4          # Delta_V_*
    slope: int = 3          # S_*_mVA
    ratio: int = 4          # Ratio_HL
    ratio_dev: int = 4      # Ratio_Dev_Pct
    projection: int = 1     # Proy_220A_*_mV
    consumo: int = 3        # Consumo_mA


LIMITS = Limits()
PRECISION = Precision()

# Absolute paths derived at import time so the service works regardless of CWD.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.join(_HERE, "..", "..")  # backend/app -> backend -> repo root
DATA_DIR = os.getenv("VA_DATA_DIR", os.path.join(_HERE, "..", "data"))
DB_PATH = os.getenv("VA_DB_PATH", os.path.join(DATA_DIR, "audit.sqlite3"))
QUARANTINE_DIR = os.getenv("VA_QUARANTINE_DIR", os.path.join(DATA_DIR, "quarantine"))
RAW_LOG_DIR = os.getenv("VA_RAW_LOG_DIR", os.path.join(DATA_DIR, "raw"))

# The ML-labeled consolidated dataset (raw logs + Ford field-failure ground truth,
# Ford_Real). This is not produced by the ingest pipeline — it's a standalone dataset
# the dashboard and the root-level ML scripts both read. Served read-only.
CONSOLIDADO_FORD_PATH = os.getenv(
    "VA_CONSOLIDADO_FORD_PATH", os.path.join(_REPO_ROOT, "CONSOLIDADO_CON_FORD.csv"))

# Static frontend (index.html + support.js), served at "/" alongside the API.
FRONTEND_DIR = os.getenv("VA_FRONTEND_DIR", _REPO_ROOT)

# Optional API-key gate on mutating routes (/api/ingest, /api/crossref). Unset (the
# default) disables auth entirely — fine for local/dev use; set VA_API_KEY before
# exposing this service beyond localhost.
API_KEY = os.getenv("VA_API_KEY") or None

# CORS origins, comma-separated. Defaults to "*" for local/dev convenience.
CORS_ORIGINS = [o.strip() for o in os.getenv("VA_CORS_ORIGINS", "*").split(",") if o.strip()]


def as_dict() -> Dict:
    """Expose the live config (used by GET /api/config for traceability)."""
    d = asdict(LIMITS)
    d["ford_limit_s_mVA"] = LIMITS.ford_limit_s_mVA
    d["VALID_COHORTS"] = list(VALID_COHORTS)
    d["precision"] = asdict(PRECISION)
    return d
