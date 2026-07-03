"""Acceptance (§6): the engine reproduces the reference dataset exactly.

We rebuild each Unit from the *raw voltage columns* of the reference
``Dataset_CONSOLIDADO.csv`` and re-run the physics/disposition, then assert:

* every recomputed Status / Razon_Falla / Ford_220A matches the reference, and
* the KPI rollups hit the acceptance numbers
  (total 1969, fordPass 1916, fordRisk 53, testerNG 1485, cohorts).

This validates the calc engine without needing the raw `.txt` logs (which are not
shipped in this repo).
"""
import csv
import os

import pytest

from app.calc import RawFields, compute
from app.consolidate import summarize

REF = os.path.join(os.path.dirname(__file__), "..", "..", "uploads",
                   "Dataset_CONSOLIDADO.csv")


def _num(x):
    x = x.strip() if x else x
    return float(x.replace(",", ".")) if x not in (None, "") else None


@pytest.fixture(scope="module")
def reference():
    if not os.path.exists(REF):
        pytest.skip(f"reference CSV not present at {REF}")
    with open(REF, encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def recomputed(reference):
    units = []
    for r in reference:
        raw = RawFields(
            Categoria=r["Categoria"],
            SerialNumber=r["SerialNumber"],
            Archivo=r["Archivo"],
            Estatus_Tester=r["Estatus_Tester"],
            Consumo_mA=_num(r["Consumo_mA"]),
            Offset_High_V=_num(r["Offset_High_V"]),
            V_20A_High_V=_num(r["V_20A_High_V"]),
            Tiempo_ms_High=_num(r["Tiempo_ms_High"]),
            Offset_Low_V=_num(r["Offset_Low_V"]),
            V_20A_Low_V=_num(r["V_20A_Low_V"]),
            Tiempo_ms_Low=_num(r["Tiempo_ms_Low"]),
        )
        units.append((r, compute(raw)))
    return units


def test_disposition_matches_reference_exactly(recomputed):
    status_mismatch = razon_mismatch = ford_mismatch = 0
    for ref, u in recomputed:
        status_mismatch += u.Status != ref["Status"]
        razon_mismatch += u.Razon_Falla != ref["Razon_Falla"]
        ford_mismatch += u.Ford_220A != ref["Ford_220A"]
    assert status_mismatch == 0
    assert razon_mismatch == 0
    assert ford_mismatch == 0


def test_rollups_match_acceptance(recomputed):
    s = summarize(u for _, u in recomputed)
    assert s["total"] == 1969
    assert s["fordPass"] == 1916
    assert s["fordRisk"] == 53
    assert s["testerNG"] == 1485
    assert s["overReject"] == 1432
    assert s["anomalies"] == 3
    cohorts = {c["cohort"]: c["total"] for c in s["byCohort"]}
    assert cohorts == {"CORRECTION_FACTOR": 949, "PRODUCCION_SOSPECHOSA": 372,
                       "SDACS": 648}


def test_risk_report_lists_exactly_53(recomputed):
    risk = [u for _, u in recomputed if u.Ford_220A == "FAIL"]
    assert len(risk) == 53
