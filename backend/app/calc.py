"""Core physics + disposition — the single formula the whole audit turns on.

    S = ΔV / 0.020 A          (slope, mV/A)
    Proy_220A = offset_mV + S * 220

The disposition rules below were validated to reproduce the reference
`Dataset_CONSOLIDADO.csv` (1,969 rows) exactly: 0 Status mismatches, 0
Razon_Falla mismatches. See tests/test_acceptance.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .config import LIMITS

# The 22-column output schema, in the exact order the dashboard's downstream
# tooling assumes (§3). Keep this list as the one place the order is defined.
CSV_COLUMNS = [
    "Categoria", "SerialNumber", "Archivo", "Estatus_Tester", "Consumo_mA",
    "Offset_High_V", "V_20A_High_V", "Tiempo_ms_High",
    "Offset_Low_V", "V_20A_Low_V", "Tiempo_ms_Low",
    "Delta_V_High", "Delta_V_Low", "S_High_mVA", "S_Low_mVA",
    "Ratio_HL", "Ratio_Dev_Pct",
    "Proy_220A_High_mV", "Proy_220A_Low_mV",
    "Status", "Razon_Falla", "Ford_220A",
]


@dataclass
class RawFields:
    """What the parser pulls out of one raw EOL log (all voltages in volts)."""
    Categoria: str
    SerialNumber: str
    Archivo: str
    Estatus_Tester: Optional[str] = None
    Consumo_mA: Optional[float] = None
    Offset_High_V: Optional[float] = None
    V_20A_High_V: Optional[float] = None
    Tiempo_ms_High: Optional[float] = None
    Offset_Low_V: Optional[float] = None
    V_20A_Low_V: Optional[float] = None
    Tiempo_ms_Low: Optional[float] = None
    # set by the parser when a required field could not be read
    parse_error: Optional[str] = None


@dataclass
class Unit:
    """A fully computed row — parsed inputs + derived physics + disposition."""
    raw: RawFields
    Delta_V_High: Optional[float] = None
    Delta_V_Low: Optional[float] = None
    S_High_mVA: Optional[float] = None
    S_Low_mVA: Optional[float] = None
    Ratio_HL: Optional[float] = None
    Ratio_Dev_Pct: Optional[float] = None
    Proy_220A_High_mV: Optional[float] = None
    Proy_220A_Low_mV: Optional[float] = None
    Status: str = "FAIL"
    Razon_Falla: str = ""
    Ford_220A: str = "FAIL"
    Anomaly: bool = False


def compute(raw: RawFields) -> Unit:
    """Turn parsed fields into a fully dispositioned Unit.

    A file that failed to parse a required field skips the physics entirely and
    is emitted as a FAIL with Razon_Falla='parse_error' (§1).
    """
    u = Unit(raw=raw)

    if raw.parse_error:
        u.Status = "FAIL"
        u.Razon_Falla = raw.parse_error  # e.g. "parse_error"
        u.Ford_220A = "FAIL"
        u.Anomaly = True
        return u

    L = LIMITS
    oh, vh = raw.Offset_High_V, raw.V_20A_High_V
    ol, vl = raw.Offset_Low_V, raw.V_20A_Low_V

    # --- slopes --------------------------------------------------------------
    if oh is not None and vh is not None:
        u.Delta_V_High = vh - oh
        u.S_High_mVA = u.Delta_V_High / L.TEST_CURRENT_A
        u.Proy_220A_High_mV = (oh * 1000.0) + (u.S_High_mVA * L.PROJECTION_CURRENT_A)
    if ol is not None and vl is not None:
        u.Delta_V_Low = vl - ol
        u.S_Low_mVA = u.Delta_V_Low / L.TEST_CURRENT_A
        u.Proy_220A_Low_mV = (ol * 1000.0) + (u.S_Low_mVA * L.PROJECTION_CURRENT_A)

    # --- ratio (guard divide-by-zero: leave None so CSV emits empty) ---------
    if u.S_High_mVA is not None and u.S_Low_mVA not in (None, 0):
        u.Ratio_HL = u.S_High_mVA / u.S_Low_mVA
        u.Ratio_Dev_Pct = (u.Ratio_HL / L.NOMINAL_RATIO - 1.0) * 100.0

    # --- disposition ---------------------------------------------------------
    u.Razon_Falla = _razon(u)
    u.Status = "FAIL" if u.Razon_Falla else "PASS"

    if u.Proy_220A_High_mV is not None:
        u.Ford_220A = "PASS" if (u.Proy_220A_High_mV / 1000.0) >= L.FORD_LIMIT_V else "FAIL"
    else:
        u.Ford_220A = "FAIL"

    u.Anomaly = u.S_High_mVA is None or u.S_High_mVA < L.ANOMALY_S_MAX
    return u


def _razon(u: Unit) -> str:
    """Build Razon_Falla exactly as the reference generator does.

    Clause order is fixed (High, Low, Ratio) and clauses join with ' | '.
    Comparisons use the *raw* float — 5.699999.. formats to '5.700' but still
    fails the >= 5.700 rule, which is why the reference has rows reading
    'S_High=5.700 < 5.7' that are nonetheless FAIL.
    """
    L = LIMITS
    parts = []
    sh, sl, ra = u.S_High_mVA, u.S_Low_mVA, u.Ratio_HL
    if sh is None:
        parts.append("S_High=missing < 5.7")
    elif sh < L.TESTER_MIN_S:
        parts.append(f"S_High={sh:.3f} < {_short(L.TESTER_MIN_S)}")
    if sl is None:
        parts.append("S_Low=missing < 19.7")
    elif sl < L.TESTER_MIN_S_LOW:
        parts.append(f"S_Low={sl:.3f} < {_short(L.TESTER_MIN_S_LOW)}")
    # ratio clause only when the ratio is defined (guarded /0 => no clause)
    if ra is not None and ra < L.RATIO_MIN:
        parts.append(f"Ratio={ra:.4f} < {_short(L.RATIO_MIN)}")
    return " | ".join(parts)


def _short(x: float) -> str:
    """Trim trailing zeros so 5.700 -> '5.7', 19.700 -> '19.7', 0.280 -> '0.28'."""
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    return s
