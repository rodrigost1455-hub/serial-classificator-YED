"""FastAPI service — turns raw EOL logs into the consolidated dataset and serves it.

Point the dashboard's CSV fetch at ``GET /api/dataset.csv`` to go live; the
response shape is identical to the static file, so no frontend change is needed.
"""
from __future__ import annotations

import csv
import io
import os
import time
import zipfile
from typing import Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response

from . import config
from .consolidate import (csv_string, risk_csv_string, summarize, unit_to_dict)
from .parser import file_hash, parse
from .schemas import (CrossRefResult, IngestResult, Summary, UnitsPage)
from .store import Store

app = FastAPI(title="BEC Virtual Audit", version="1.0")
app.add_middleware(GZipMiddleware, minimum_size=1024)          # §5: gzip the CSV
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["*"],
                   allow_headers=["*"])

store = Store()


def require_api_key(x_api_key: Optional[str] = Header(None)):
    """Gate mutating routes when VA_API_KEY is set; a no-op otherwise (local/dev)."""
    if config.API_KEY and x_api_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")


def _persist_raw(content: bytes, filename: str, quarantine: bool) -> str:
    """Save the raw log for the audit trail; quarantined files go to their own dir."""
    target_dir = config.QUARANTINE_DIR if quarantine else config.RAW_LOG_DIR
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, filename)
    try:
        with open(path, "wb") as f:
            f.write(content)
    except OSError:
        pass
    return path


def _ingest_one(content: bytes, filename: str, cohort: Optional[str]) -> str:
    """Parse + store one log. Returns 'added' | 'skipped' | 'quarantined'."""
    fh = file_hash(content)
    raw = parse(content, filename, cohort)
    quarantined = bool(raw.parse_error)
    path = _persist_raw(content, filename, quarantined)
    result = store.add(raw, fh, raw_path=path, mtime=time.time())
    if result == "added" and quarantined:
        return "quarantined"
    return result


@app.get("/api/health")
def health():
    return {"status": "ok", "units": store.count()}


@app.get("/api/config")
def get_config():
    """Live limits/cohorts — traceability for what rules produced a disposition."""
    return config.as_dict()


@app.post("/api/ingest", response_model=IngestResult, dependencies=[Depends(require_api_key)])
async def ingest(files: list[UploadFile] = File(...),
                 cohort: Optional[str] = Query(None)):
    """Upload one or more `.txt` logs (or a `.zip` of them). Idempotent."""
    added = skipped = quarantined = seen = 0
    for up in files:
        content = await up.read()
        if up.filename.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                for name in z.namelist():
                    if name.lower().endswith(".txt") and not name.endswith("/"):
                        seen += 1
                        r = _ingest_one(z.read(name), os.path.basename(name), cohort)
                        added += r == "added"
                        skipped += r == "skipped"
                        quarantined += r == "quarantined"
        else:
            seen += 1
            r = _ingest_one(content, up.filename, cohort)
            added += r == "added"
            skipped += r == "skipped"
            quarantined += r == "quarantined"
    return IngestResult(added=added, skipped=skipped, quarantined=quarantined, files=seen)


@app.get("/api/dataset.csv")
def dataset_csv():
    """Full consolidated CSV, from raw-log ingest — what the ingest pipeline produces."""
    body = csv_string(store.iter_units(active_only=True))
    return PlainTextResponse(body, media_type="text/csv",
                             headers={"Content-Disposition":
                                      "attachment; filename=Dataset_CONSOLIDADO.csv"})


@app.get("/api/consolidado-ford.csv")
def consolidado_ford_csv():
    """The ML-labeled dataset (adds Fecha/Ford_Real) — what the dashboard and the
    root-level ML scripts read. A static passthrough, not derived from the ingest
    store: this file carries Ford's confirmed field-failure ground truth, which
    can't be computed from a tester log."""
    path = config.CONSOLIDADO_FORD_PATH
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="CONSOLIDADO_CON_FORD.csv not found")
    return FileResponse(path, media_type="text/csv",
                        headers={"Content-Disposition":
                                 "attachment; filename=CONSOLIDADO_CON_FORD.csv"})


@app.get("/api/risk-report.csv")
def risk_report_csv():
    """Only Ford_220A=FAIL rows — the action list."""
    body = risk_csv_string(store.iter_units(active_only=True))
    return PlainTextResponse(body, media_type="text/csv",
                             headers={"Content-Disposition":
                                      "attachment; filename=risk-report.csv"})


@app.get("/api/summary", response_model=Summary)
def summary():
    return summarize(store.iter_units(active_only=True))


@app.get("/api/units", response_model=UnitsPage)
def units(cohort: Optional[str] = None, status: Optional[str] = None,
          ford: Optional[str] = None, q: Optional[str] = None,
          limit: int = Query(100, ge=1, le=5000), offset: int = Query(0, ge=0)):
    """Paginated JSON for large fleets. Filters are ANDed; `q` matches serial substring."""
    def keep(d: dict) -> bool:
        if cohort and d["Categoria"] != cohort:
            return False
        if status and d["Status"] != status.upper():
            return False
        if ford and d["Ford_220A"] != ford.upper():
            return False
        if q and q not in d["SerialNumber"]:
            return False
        return True

    matched, page = 0, []
    for u in store.iter_units(active_only=True):
        d = unit_to_dict(u)
        if not keep(d):
            continue
        if offset <= matched < offset + limit:
            page.append(d)
        matched += 1
    return UnitsPage(total=matched, limit=limit, offset=offset, rows=page)


@app.post("/api/crossref", response_model=CrossRefResult, dependencies=[Depends(require_api_key)])
async def crossref(file: UploadFile = File(...)):
    """Join a SAP/shipment file [Serial, Ship_Date, Destination_Plant] against the
    fleet to flag already-shipped at-risk (Ford_220A=FAIL) units."""
    content = (await file.read()).decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(content))
    # tolerate header-name variants
    def pick(row, *names):
        for n in names:
            for k in row:
                if k and k.strip().lower() == n.lower():
                    return row[k]
        return None

    shipped = {}
    for row in reader:
        serial = pick(row, "Serial", "SerialNumber", "Numero_Serie")
        if serial:
            shipped[serial.strip()] = {
                "Ship_Date": pick(row, "Ship_Date", "FechaEnvio", "Ship Date"),
                "Destination_Plant": pick(row, "Destination_Plant", "Planta", "Destination Plant"),
            }

    hits = []
    matched = 0
    for u in store.iter_units(active_only=True):
        info = shipped.get(u.raw.SerialNumber)
        if info is None:
            continue
        matched += 1
        if u.Ford_220A == "FAIL":
            hits.append({
                "SerialNumber": u.raw.SerialNumber,
                "Ship_Date": info["Ship_Date"],
                "Destination_Plant": info["Destination_Plant"],
                "Ford_220A": u.Ford_220A,
                "Status": u.Status,
                "S_High_mVA": u.S_High_mVA,
                "Proy_220A_High_mV": u.Proy_220A_High_mV,
            })
    return CrossRefResult(shipped_rows=len(shipped), matched=matched,
                          at_risk=len(hits), hits=hits)


# Static dashboard, served same-origin so the browser fetch to
# /api/consolidado-ford.csv needs no CORS. Only these two files are exposed —
# never mount config.FRONTEND_DIR wholesale, it's the repo root and would leak
# backend source, the sqlite DB, and raw logs over HTTP.
@app.get("/")
def dashboard_index():
    return FileResponse(os.path.join(config.FRONTEND_DIR, "index.html"), media_type="text/html")


@app.get("/support.js")
def dashboard_support_js():
    return FileResponse(os.path.join(config.FRONTEND_DIR, "support.js"),
                        media_type="application/javascript")
