# Backend Prompt — BEC Current Sensor "Virtual Audit"

> Paste this to your backend engineer or coding agent. It specifies the service that
> turns raw end-of-line (EOL) tester logs into the **consolidated CSV** the Virtual
> Audit dashboard consumes, plus the calculations and API. The frontend already reads
> `Dataset_CONSOLIDADO.csv` — your job is to **produce that file reliably** and serve it.

---

## 0. Context (one paragraph)

Yazaki YED ships BEC current sensors for Ford PHEV Gen-4. Ford rejected units in the
field that **passed our EOL tester at 20 A**. Root cause: a **sensitivity** (slope)
defect that is invisible at 20 A but blows past Ford's tolerance at **220 A**. The
single insight is one formula: `S = ΔV / 0.020 A`. The backend's job is to compute `S`
and the 220 A projection for **every serial we have ever tested**, flag the ones that
will fail Ford, and emit a consolidated CSV for the dashboard. No new hardware, no
change to the tester — pure post-processing of logs we already capture.

---

## 1. Input — raw EOL log files (`.txt`)

One file per test run. Filename carries the serial: `7370-2573-8W <SERIAL> 0101 OK.txt`
(a `(1)` suffix or `NG` token may appear on retests). Extract these fields per file:

| Field | Source section in the log | Notes |
|---|---|---|
| `SerialNumber` | filename (the 11-digit run, e.g. `26168000568`) | de-dupe; keep latest by file mtime |
| `Estatus_Tester` | tester verdict line (`OK` / `NG`) | the tester's own binary result |
| `Consumo_mA` | `CONSUMPTION CURRENT` | mA |
| `Offset_High_V` | `HIGH RANGE OUTPUT VOLTAGE` | volts, ≈ 2.50 (V at 0 A) |
| `V_20A_High_V` | `POLARITY` → sub `VOLTAJE C3-C2` | volts at 20 A, High range |
| `Tiempo_ms_High` | 4th field of `MedirVoltajeDMM` for the High reading | stabilization ms |
| `Offset_Low_V` | `LOW RANGE OUTPUT VOLTAGE` | volts |
| `V_20A_Low_V` | `POLARITY` → sub `VOLTAJE C3-C4` | volts at 20 A, Low range |
| `Tiempo_ms_Low` | 4th field of `MedirVoltajeDMM` for the Low reading | stabilization ms |

Be defensive: logs vary in whitespace/locale (commas as decimals appear in some). Parse
numbers tolerant of `,`/`.`; if a required field is missing, route the file to a
`quarantine/` folder and emit a row with `Status=FAIL, Razon_Falla="parse_error"`.

### Cohort tagging (`Categoria`)
Each batch of logs belongs to one cohort — set from the source folder / import job:
- `CORRECTION_FACTOR` — baseline production used to derive the limit
- `PRODUCCION_SOSPECHOSA` — suspect lots under investigation
- `SDACS` — the SDACS line/program

---

## 2. The calculation (core physics — do this exactly)

All voltages in **volts**, currents in **amps**, `S` in **mV/A**, projections in **mV**.

```
Delta_V_High = V_20A_High_V - Offset_High_V          # volts
Delta_V_Low  = V_20A_Low_V  - Offset_Low_V

S_High_mVA   = Delta_V_High / 0.020                  # volts / 0.020 A  ->  mV/A
S_Low_mVA    = Delta_V_Low  / 0.020

Proy_220A_High_mV = (Offset_High_V * 1000) + (S_High_mVA * 220)   # mV at 220 A
Proy_220A_Low_mV  = (Offset_Low_V  * 1000) + (S_Low_mVA  * 220)

Ratio_HL      = S_High_mVA / S_Low_mVA  (guard /0)
Ratio_Dev_Pct = (Ratio_HL / NOMINAL_RATIO - 1) * 100             # NOMINAL_RATIO ≈ 0.2885
```

### Disposition rules (single source of truth — config, not hardcode)
```
TESTER_MIN_S   = 5.700      # mV/A  -> our EOL acceptance rule
FORD_LIMIT_V   = 3.746      # V     -> Ford's 220 A lower limit

Status     = "PASS" if S_High_mVA >= TESTER_MIN_S else "FAIL"
             Razon_Falla = "S_High={S:.3f} < 5.7" (or "S_Low=0", "polarity_inverted", ...)
Ford_220A  = "PASS" if (Proy_220A_High_mV / 1000) >= FORD_LIMIT_V else "FAIL"
Anomaly    = S_High_mVA < 1.0   # dead / inverted polarity / S≈0
```

> **Key business insight to preserve:** `Status` (the 5.700 rule) intentionally
> over-rejects — in the reference set it flags ~1,485 units, but only **53** actually
> fail Ford (`Ford_220A=FAIL`). The audit's value is isolating those 53 real escapes.
> Keep both columns; never collapse them.

---

## 3. Output — `Dataset_CONSOLIDADO.csv` (exact schema, header row required)

One row per processed log. Column order **must** match — the dashboard maps by header name
but downstream tooling assumes this order:

```
Categoria,SerialNumber,Archivo,Estatus_Tester,Consumo_mA,
Offset_High_V,V_20A_High_V,Tiempo_ms_High,Offset_Low_V,V_20A_Low_V,Tiempo_ms_Low,
Delta_V_High,Delta_V_Low,S_High_mVA,S_Low_mVA,Ratio_HL,Ratio_Dev_Pct,
Proy_220A_High_mV,Proy_220A_Low_mV,Status,Razon_Falla,Ford_220A
```

Rules: UTF-8, `\n` line endings, `.` decimal separator, no thousands separators. Quote any
field containing a comma (e.g. `Razon_Falla`). Numeric precision: voltages 3–4 dp, `S` 3 dp,
projections 0–1 dp. Empty `Razon_Falla` for passes. Duplicate serials allowed (retests) but
mark the superseded run.

---

## 4. API (so the dashboard isn't tied to a static file)

| Method | Route | Returns |
|---|---|---|
| `POST` | `/api/ingest` | multipart upload of `.txt` logs (or a `.zip`); parses, computes, appends; returns `{added, skipped, quarantined}` |
| `GET`  | `/api/dataset.csv` | the full consolidated CSV (what the dashboard fetches) |
| `GET`  | `/api/units?cohort=&status=&ford=&q=&limit=&offset=` | paginated JSON for large fleets (>50k rows) |
| `GET`  | `/api/summary` | KPI rollups: `{total, fordPass, fordRisk, testerNG, overReject, anomalies, meanS, fordLimitS, byCohort:[...]}` |
| `GET`  | `/api/risk-report.csv` | only `Ford_220A=FAIL` rows — the action list |
| `POST` | `/api/crossref` | optional SAP/shipment file `[Serial, Ship_Date, Destination_Plant]`; joins by serial to flag already-shipped at-risk units |

The dashboard currently `fetch`es a CSV; point it at `/api/dataset.csv` to go live.
Keep the response shape identical so no frontend change is needed.

---

## 5. Non-functional

- **Idempotent ingest** — re-uploading the same log must not duplicate; key on
  `(SerialNumber, file hash)`. Latest run wins for dashboard counts.
- **Limits are config** — `TESTER_MIN_S`, `FORD_LIMIT_V`, `NOMINAL_RATIO`, cohort map in
  one config file / table. Quality Engineering will tune these.
- **Audit trail** — store raw log path + parsed fields + computed values so any flagged
  serial is fully traceable back to its log (Ford/8D requirement).
- **Scale** — design for hundreds of thousands of logs; stream-parse, don't load all in
  memory; the CSV endpoint should support gzip.
- **Validation** — unit-test the math against known cases:
  `S=5.700 → Proj≈3759 mV → Ford PASS`; `S=5.600 → Proj≈3737 mV → Ford FAIL`;
  `S=5.650 → borderline (≈3745–3748, depends on offset)`; `S=0 → anomaly`.
- Stack-agnostic (Python/FastAPI + pandas, or Node/Express, or .NET) — the contract above
  is what matters.

---

## 6. Acceptance

Given the reference log set, the service reproduces a CSV whose rollups match:
**total ≈ 1,969 · Ford Pass ≈ 1,916 · Ford Risk = 53 · Tester NG = 1,485 ·
cohorts {CORRECTION_FACTOR, PRODUCCION_SOSPECHOSA, SDACS}**, and `/api/risk-report.csv`
lists exactly the 53 serials whose `Ford_220A=FAIL`.
