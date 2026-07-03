"""End-to-end API tests against an isolated on-disk store."""
import os
import tempfile

import pytest

# Point the store at a throwaway DB before the app imports config paths.
_TMP = tempfile.mkdtemp(prefix="va_test_")
os.environ["VA_DATA_DIR"] = _TMP
os.environ["VA_DB_PATH"] = os.path.join(_TMP, "test.sqlite3")
os.environ["VA_QUARANTINE_DIR"] = os.path.join(_TMP, "quarantine")
os.environ["VA_RAW_LOG_DIR"] = os.path.join(_TMP, "raw")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, store  # noqa: E402

client = TestClient(app)
FIX = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture(autouse=True)
def clean_store():
    store.clear()
    yield


def _upload(*names, cohort="CORRECTION_FACTOR"):
    files = []
    for n in names:
        with open(os.path.join(FIX, n), "rb") as f:
            files.append(("files", (n, f.read(), "text/plain")))
    return client.post(f"/api/ingest?cohort={cohort}", files=files)


def test_ingest_and_dataset_csv():
    r = _upload("7370-2573-8W 26168000568 0101 OK.txt")
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 1 and body["quarantined"] == 0

    csv_resp = client.get("/api/dataset.csv")
    assert csv_resp.status_code == 200
    text = csv_resp.text
    assert text.startswith("Categoria,SerialNumber,")
    assert "26168000568" in text
    assert "\r\n" not in text  # \n line endings only


def test_ingest_is_idempotent():
    _upload("7370-2573-8W 26168000568 0101 OK.txt")
    r = _upload("7370-2573-8W 26168000568 0101 OK.txt")
    assert r.json()["skipped"] == 1
    assert r.json()["added"] == 0


def test_parse_error_is_quarantined():
    r = _upload("7370-2573-8W 26168000404 0101 OK.txt")
    assert r.json()["quarantined"] == 1
    # the quarantined unit still appears as a FAIL row
    units = client.get("/api/units").json()
    row = units["rows"][0]
    assert row["Razon_Falla"] == "parse_error"
    assert row["Status"] == "FAIL"


def test_summary_shape():
    _upload("7370-2573-8W 26168000568 0101 OK.txt")
    s = client.get("/api/summary").json()
    for key in ("total", "fordPass", "fordRisk", "testerNG", "overReject",
                "anomalies", "meanS", "fordLimitS", "byCohort"):
        assert key in s
    assert s["total"] == 1


def test_units_filtering():
    _upload("7370-2573-8W 26168000568 0101 OK.txt")
    _upload("7370-2573-8W 26168000999 0101 NG.txt")
    all_rows = client.get("/api/units").json()
    assert all_rows["total"] == 2
    q = client.get("/api/units?q=568").json()
    assert q["total"] == 1
    assert q["rows"][0]["SerialNumber"] == "26168000568"


def test_risk_report_csv():
    _upload("7370-2573-8W 26168000999 0101 NG.txt")  # S~5.60 -> Ford FAIL
    r = client.get("/api/risk-report.csv")
    assert r.status_code == 200
    assert "26168000999" in r.text


def test_crossref_flags_shipped_at_risk():
    _upload("7370-2573-8W 26168000999 0101 NG.txt")  # at risk
    ship = "Serial,Ship_Date,Destination_Plant\n26168000999,2026-03-01,Ford-Dearborn\n"
    r = client.post("/api/crossref",
                    files={"file": ("ship.csv", ship.encode(), "text/csv")})
    body = r.json()
    assert body["matched"] == 1
    assert body["at_risk"] == 1
    assert body["hits"][0]["Destination_Plant"] == "Ford-Dearborn"
