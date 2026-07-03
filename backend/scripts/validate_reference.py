"""Standalone check: re-derive dispositions from the reference CSV and print the
rollups next to the acceptance targets. Run: `python -m scripts.validate_reference`
from the backend/ directory.
"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.calc import RawFields, compute          # noqa: E402
from app.consolidate import summarize            # noqa: E402

REF = os.path.join(os.path.dirname(__file__), "..", "..", "uploads",
                   "Dataset_CONSOLIDADO.csv")
TARGETS = {"total": 1969, "fordPass": 1916, "fordRisk": 53, "testerNG": 1485}


def _num(x):
    x = x.strip() if x else x
    return float(x.replace(",", ".")) if x not in (None, "") else None


def main():
    with open(REF, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    units, sm, rm = [], 0, 0
    for r in rows:
        u = compute(RawFields(
            Categoria=r["Categoria"], SerialNumber=r["SerialNumber"],
            Archivo=r["Archivo"], Estatus_Tester=r["Estatus_Tester"],
            Consumo_mA=_num(r["Consumo_mA"]),
            Offset_High_V=_num(r["Offset_High_V"]), V_20A_High_V=_num(r["V_20A_High_V"]),
            Offset_Low_V=_num(r["Offset_Low_V"]), V_20A_Low_V=_num(r["V_20A_Low_V"]),
        ))
        sm += u.Status != r["Status"]
        rm += u.Razon_Falla != r["Razon_Falla"]
        units.append(u)

    s = summarize(units)
    print(f"Status mismatches : {sm}")
    print(f"Razon  mismatches : {rm}")
    print("-" * 40)
    for k, target in TARGETS.items():
        ok = "OK " if s[k] == target else "XX "
        print(f"[{ok}] {k:10s} = {s[k]:>6}  (target {target})")
    print(f"      byCohort   = {s['byCohort']}")
    print(f"      meanS      = {s['meanS']}   fordLimitS = {s['fordLimitS']}")
    return 0 if sm == 0 and rm == 0 and all(s[k] == t for k, t in TARGETS.items()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
