"""Math validated against the known cases in §5 of the spec."""
import math

from app.calc import RawFields, compute
from app.config import LIMITS


def _unit(vh, vl=2.904, oh=2.505, ol=2.504):
    return compute(RawFields(
        Categoria="CORRECTION_FACTOR", SerialNumber="X", Archivo="x.txt",
        Estatus_Tester="OK",
        Offset_High_V=oh, V_20A_High_V=vh, Offset_Low_V=ol, V_20A_Low_V=vl,
    ))


def test_slope_and_projection_formula():
    u = _unit(vh=2.619)  # delta 0.114 -> S 5.70
    assert math.isclose(u.S_High_mVA, 5.700, abs_tol=1e-3)
    assert math.isclose(u.Proy_220A_High_mV, 3759.0, abs_tol=0.5)


def test_S570_ford_pass():
    # S=5.700 -> Proj ~3759 mV -> Ford PASS
    assert _unit(vh=2.619).Ford_220A == "PASS"


def test_S560_ford_fail():
    # S=5.600 -> Proj ~3737 mV -> Ford FAIL
    u = _unit(vh=2.617)  # delta 0.112 -> S 5.60
    assert math.isclose(u.S_High_mVA, 5.600, abs_tol=1e-3)
    assert math.isclose(u.Proy_220A_High_mV, 3737.0, abs_tol=0.5)
    assert u.Ford_220A == "FAIL"


def test_S565_borderline_ford_pass():
    # S=5.650 -> Proj ~3748 mV -> just above Ford's 3746 limit
    u = _unit(vh=2.618)  # delta 0.113 -> S 5.65
    assert 3745 <= u.Proy_220A_High_mV <= 3749
    assert u.Ford_220A == "PASS"


def test_S_zero_is_anomaly():
    u = _unit(vh=2.505, vl=2.504)  # both deltas 0
    assert u.S_High_mVA == 0.0
    assert u.Anomaly is True
    assert u.Status == "FAIL"
    assert u.Ford_220A == "FAIL"


def test_ratio_and_deviation():
    u = _unit(vh=2.619)  # S_High 5.70, S_Low 20.0 -> ratio 0.285
    assert math.isclose(u.Ratio_HL, 0.285, abs_tol=1e-3)
    # dev vs nominal 0.2885
    expected = (u.Ratio_HL / LIMITS.NOMINAL_RATIO - 1) * 100
    assert math.isclose(u.Ratio_Dev_Pct, expected, abs_tol=1e-9)


def test_divide_by_zero_guard_leaves_ratio_none():
    u = _unit(vh=2.619, vl=2.504)  # S_Low = 0
    assert u.S_Low_mVA == 0.0
    assert u.Ratio_HL is None
    assert u.Ratio_Dev_Pct is None
    assert "Ratio=" not in u.Razon_Falla  # no ratio clause when guarded


def test_parse_error_unit():
    raw = RawFields(Categoria="SDACS", SerialNumber="", Archivo="bad.txt",
                    parse_error="parse_error")
    u = compute(raw)
    assert u.Status == "FAIL"
    assert u.Razon_Falla == "parse_error"
    assert u.Ford_220A == "FAIL"
