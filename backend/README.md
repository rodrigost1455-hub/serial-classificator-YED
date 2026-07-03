# BEC Current Sensor — Virtual Audit Backend

Turns raw end-of-line (EOL) tester logs into the consolidated dataset the Virtual
Audit dashboard consumes, computes the 220 A projection for every serial, flags the
units that will fail Ford, and serves it all over a small REST API.

The single insight the whole service turns on:

```
S = ΔV / 0.020 A                         # slope, mV/A
Proy_220A = offset_mV + S · 220          # projected voltage at Ford's 220 A
```

## Quick start

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate            # Windows;  source .venv/bin/activate on *nix
pip install -r requirements.txt

uvicorn app.main:app --reload     # http://127.0.0.1:8000
```

Interactive docs at `http://127.0.0.1:8000/docs`.

**Go live on the dashboard:** point its CSV fetch at `GET /api/dataset.csv`. The
response is byte-compatible with the static `Dataset_CONSOLIDADO.csv`, so no
frontend change is needed.

## API

| Method | Route | Purpose |
|---|---|---|
| `POST` | `/api/ingest?cohort=` | Upload `.txt` logs or a `.zip`. Idempotent. → `{added, skipped, quarantined, files}` |
| `GET`  | `/api/dataset.csv` | Full consolidated CSV (gzip-negotiated) |
| `GET`  | `/api/risk-report.csv` | Only `Ford_220A=FAIL` rows — the action list |
| `GET`  | `/api/summary` | KPI rollups (see below) |
| `GET`  | `/api/units?cohort=&status=&ford=&q=&limit=&offset=` | Paginated JSON for large fleets |
| `POST` | `/api/crossref` | Join a SAP/shipment file `[Serial, Ship_Date, Destination_Plant]` to flag shipped at-risk units |
| `GET`  | `/api/config` | Live limits/cohorts (traceability) |
| `GET`  | `/api/health` | Liveness + unit count |
| `GET`  | `/api/consolidado-ford.csv` | Static passthrough of `CONSOLIDADO_CON_FORD.csv` (has `Ford_Real` ground truth) — what the dashboard and ML scripts read |
| `GET`  | `/api/ml-rank.csv` | Zona AMARILLO only, ranked by ML priority score (`ML_Risk_Score`/`Rank`) — a ranking aid, never a disposition. See `app/ml_rank.py` |
| `GET`  | `/api/ml-rank/meta` | Training report for the ranking model (PR-AUC, ROC-AUC, gain@10/25/50%) |

`GET /api/summary` returns:

```json
{ "total", "fordPass", "fordRisk", "testerNG", "overReject",
  "anomalies", "meanS", "fordLimitS", "byCohort": [...] }
```

* **testerNG** — flagged by our 5.700 rule (`Status=FAIL`); it *intentionally
  over-rejects*.
* **fordRisk** — the real escapes (`Ford_220A=FAIL`); the 53 units that matter.
* **overReject** — `testerNG − fordRisk`: the cost of over-rejection.

## The rules (reverse-engineered from, and verified against, the reference set)

`Status`/`Razon_Falla` are **not** the single-clause rule in the original prompt —
the reference `Dataset_CONSOLIDADO.csv` encodes three sub-rules, joined by ` | `:

| Clause | Threshold (config) | Emitted as |
|---|---|---|
| High slope | `S_High < TESTER_MIN_S` (5.700) | `S_High=5.700 < 5.7` |
| Low slope  | `S_Low  < TESTER_MIN_S_LOW` (19.700) | `S_Low=0.000 < 19.7` |
| Ratio      | `Ratio_HL < RATIO_MIN` (0.28), skipped when guarded /0 | `Ratio=0.2789 < 0.28` |

`Status = FAIL` if any clause fires. Comparisons use the **raw float** (so
`5.6999… < 5.7` fails even though it prints as `5.700`). `Ford_220A = PASS` iff
`Proy_220A_High_mV / 1000 ≥ FORD_LIMIT_V` (3.746 V).

All thresholds live in [`app/config.py`](app/config.py) and are overridable via
environment variables (`TESTER_MIN_S`, `FORD_LIMIT_V`, `NOMINAL_RATIO`, …). QE can
retune without a code change — dispositions are recomputed from live config on read.

## Validation

```bash
python -m scripts.validate_reference     # re-derives dispositions from the reference CSV
pytest                                    # 24 tests
```

The acceptance test rebuilds every unit from the reference CSV's raw voltages and
asserts the engine reproduces it exactly and hits the §6 targets:

```
Status mismatches : 0      total    = 1969
Razon  mismatches : 0      fordPass = 1916
                           fordRisk = 53      testerNG = 1485
cohorts { CORRECTION_FACTOR: 949, SDACS: 648, PRODUCCION_SOSPECHOSA: 372 }
```

## Architecture

```
app/
  config.py       limits, cohort map, precision  (single source of truth)
  parser.py       raw .txt -> RawFields           (label-based, locale-tolerant)
  calc.py         RawFields -> Unit               (physics + disposition)
  store.py        SQLite audit store              (idempotent, latest-run-wins)
  consolidate.py  Unit(s) -> CSV / JSON / rollups
  main.py         FastAPI app
```

**Idempotent ingest** keys on the raw file's sha256; re-uploading is a no-op.
**Retests** sharing a serial keep the newest run active (by mtime); older runs are
marked superseded and excluded from dashboard counts but retained for the 8D audit
trail. Only raw parsed fields are persisted; the physics is recomputed on read, so
reads stream row-by-row and a limits change takes effect with no re-ingest.

### On the raw-log parser

The real tester logs are **not** included in this repo (only the consolidated
output CSV is). The parser locates each value by the section label the spec names
(`CONSUMPTION CURRENT`, `HIGH/LOW RANGE OUTPUT VOLTAGE`, `POLARITY → VOLTAJE
C3-C2 / C3-C4`, 4th field of `MedirVoltajeDMM`) and reads the number that follows,
tolerant of `,`/`.` decimals. The label patterns in `parser.py` (`LABELS`) are the
one place to adjust if the actual log layout differs — the extraction logic won't
need to change. Files missing any of the four required voltages are copied to
`data/quarantine/` and emitted as a `Razon_Falla=parse_error` FAIL row.
