"""Parser reads the labelled sections and is tolerant of comma decimals."""
import os

from app.parser import extract_serial, parse, parse_number

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _read(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read(), name


def test_extract_serial():
    assert extract_serial("7370-2573-8W 26168000568 0101 OK.txt") == "26168000568"
    assert extract_serial("no-serial-here.txt") is None


def test_parse_number_tolerates_comma():
    # parse_number reads the first number in the (post-label) remainder
    assert parse_number(": 2,619") == 2.619
    assert parse_number(": 2.505 V") == 2.505
    assert parse_number("no number") is None


def test_parse_ok_file():
    content, name = _read("7370-2573-8W 26168000568 0101 OK.txt")
    raw = parse(content, name, cohort="CORRECTION_FACTOR")
    assert raw.parse_error is None
    assert raw.SerialNumber == "26168000568"
    assert raw.Estatus_Tester == "OK"
    assert raw.Consumo_mA == 21.166
    assert raw.Offset_High_V == 2.505
    assert raw.V_20A_High_V == 2.619
    assert raw.Offset_Low_V == 2.504
    assert raw.V_20A_Low_V == 2.904
    assert raw.Tiempo_ms_High == 19.0   # first MedirVoltajeDMM, 4th field
    assert raw.Tiempo_ms_Low == 21.0    # second reading


def test_parse_comma_locale_file():
    content, name = _read("7370-2573-8W 26168000999 0101 NG.txt")
    raw = parse(content, name)
    assert raw.parse_error is None
    assert raw.Estatus_Tester == "NG"
    assert raw.Offset_High_V == 2.505
    assert raw.V_20A_High_V == 2.617


def test_parse_missing_voltage_quarantines():
    content, name = _read("7370-2573-8W 26168000404 0101 OK.txt")
    raw = parse(content, name)
    assert raw.parse_error == "parse_error"  # C3-C2 / C3-C4 absent


def test_cohort_defaults_when_invalid():
    content, name = _read("7370-2573-8W 26168000568 0101 OK.txt")
    raw = parse(content, name, cohort="NONSENSE")
    assert raw.Categoria == "CORRECTION_FACTOR"
