"""Emit `Dataset_CONSOLIDADO.csv` and compute the dashboard rollups.

CSV rules (§3): UTF-8, '\\n' line endings, '.' decimals, no thousands separators,
fields with commas are quoted (the csv module handles quoting). Column order is
pinned by ``calc.CSV_COLUMNS``.
"""
from __future__ import annotations

import csv
import io
from typing import Dict, Iterable, Iterator, List, Optional

from .calc import CSV_COLUMNS, Unit
from .config import LIMITS, PRECISION


def _fmt(x: Optional[float], places: int) -> str:
    if x is None:
        return ""
    return f"{x:.{places}f}"


def unit_to_row(u: Unit) -> List[str]:
    r = u.raw
    P = PRECISION
    return [
        r.Categoria,
        r.SerialNumber,
        r.Archivo,
        r.Estatus_Tester or "",
        _fmt(r.Consumo_mA, P.consumo),
        _fmt(r.Offset_High_V, P.voltage),
        _fmt(r.V_20A_High_V, P.voltage),
        _fmt(r.Tiempo_ms_High, 1),
        _fmt(r.Offset_Low_V, P.voltage),
        _fmt(r.V_20A_Low_V, P.voltage),
        _fmt(r.Tiempo_ms_Low, 1),
        _fmt(u.Delta_V_High, P.delta),
        _fmt(u.Delta_V_Low, P.delta),
        _fmt(u.S_High_mVA, P.slope),
        _fmt(u.S_Low_mVA, P.slope),
        _fmt(u.Ratio_HL, P.ratio),
        _fmt(u.Ratio_Dev_Pct, P.ratio_dev),
        _fmt(u.Proy_220A_High_mV, P.projection),
        _fmt(u.Proy_220A_Low_mV, P.projection),
        u.Status,
        u.Razon_Falla,
        u.Ford_220A,
    ]


def write_csv(units: Iterable[Unit], fp: io.StringIO) -> int:
    """Write header + rows to a text stream. Returns row count."""
    w = csv.writer(fp, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    n = 0
    for u in units:
        w.writerow(unit_to_row(u))
        n += 1
    return n


def csv_string(units: Iterable[Unit]) -> str:
    buf = io.StringIO()
    write_csv(units, buf)
    return buf.getvalue()


def risk_csv_string(units: Iterable[Unit]) -> str:
    """Only Ford_220A=FAIL rows — the action list (/api/risk-report.csv)."""
    return csv_string(u for u in units if u.Ford_220A == "FAIL")


def unit_to_dict(u: Unit) -> Dict:
    """Numeric (unformatted) view for JSON endpoints — same keys as the CSV."""
    r = u.raw
    return {
        "Categoria": r.Categoria,
        "SerialNumber": r.SerialNumber,
        "Archivo": r.Archivo,
        "Estatus_Tester": r.Estatus_Tester,
        "Consumo_mA": r.Consumo_mA,
        "Offset_High_V": r.Offset_High_V,
        "V_20A_High_V": r.V_20A_High_V,
        "Tiempo_ms_High": r.Tiempo_ms_High,
        "Offset_Low_V": r.Offset_Low_V,
        "V_20A_Low_V": r.V_20A_Low_V,
        "Tiempo_ms_Low": r.Tiempo_ms_Low,
        "Delta_V_High": u.Delta_V_High,
        "Delta_V_Low": u.Delta_V_Low,
        "S_High_mVA": u.S_High_mVA,
        "S_Low_mVA": u.S_Low_mVA,
        "Ratio_HL": u.Ratio_HL,
        "Ratio_Dev_Pct": u.Ratio_Dev_Pct,
        "Proy_220A_High_mV": u.Proy_220A_High_mV,
        "Proy_220A_Low_mV": u.Proy_220A_Low_mV,
        "Status": u.Status,
        "Razon_Falla": u.Razon_Falla,
        "Ford_220A": u.Ford_220A,
        "Anomaly": u.Anomaly,
    }


def summarize(units: Iterable[Unit]) -> Dict:
    """KPI rollups for /api/summary.

    * testerNG   — flagged by our 5.700 rule (Status=FAIL); intentionally over-rejects
    * fordRisk   — real escapes (Ford_220A=FAIL); the 53 units that matter
    * overReject — Status=FAIL but Ford_220A=PASS (the cost of the over-rejection)
    """
    total = tester_ng = ford_pass = ford_risk = anomalies = 0
    s_sum = 0.0
    s_n = 0
    by_cohort: Dict[str, Dict[str, int]] = {}

    for u in units:
        total += 1
        c = by_cohort.setdefault(u.raw.Categoria,
                                 {"total": 0, "fordRisk": 0, "testerNG": 0})
        c["total"] += 1
        if u.Status == "FAIL":
            tester_ng += 1
            c["testerNG"] += 1
        if u.Ford_220A == "PASS":
            ford_pass += 1
        else:
            ford_risk += 1
            c["fordRisk"] += 1
        if u.Anomaly:
            anomalies += 1
        if u.S_High_mVA is not None:
            s_sum += u.S_High_mVA
            s_n += 1

    return {
        "total": total,
        "fordPass": ford_pass,
        "fordRisk": ford_risk,
        "testerNG": tester_ng,
        "overReject": tester_ng - ford_risk,  # flagged by us but Ford-OK
        "anomalies": anomalies,
        "meanS": round(s_sum / s_n, 4) if s_n else None,
        "fordLimitS": round(LIMITS.ford_limit_s_mVA, 4),
        "byCohort": [
            {"cohort": k, **v} for k, v in sorted(by_cohort.items())
        ],
    }
