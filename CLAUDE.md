# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Yazaki YED's "Virtual Audit" for the Ford BEC current sensor recall. Ford rejected units in
the field that passed the EOL tester at 20 A; root cause is a sensitivity (slope) defect
invisible at 20 A but out of tolerance at Ford's 220 A operating point. Everything here turns
on one formula:

```
S = ΔV / 0.020 A                         # slope, mV/A
Proy_220A = offset_mV + S · 220          # projected voltage at Ford's 220 A
```

The repo has three pieces, tied together by the backend:

1. **`backend/`** — a FastAPI service with two jobs: (a) parse raw EOL `.txt` tester logs
   into a consolidated dataset (recomputing physics/disposition on every read), and
   (b) serve `CONSOLIDADO_CON_FORD.csv` — a separate, pre-built dataset with Ford's
   confirmed field-failure ground truth — as a static passthrough. It also serves the
   dashboard itself, same-origin. This is the actively-developed, tested part of the repo.
2. **Root-level dashboard** (`index.html` + `support.js`) — fetches
   `/api/consolidado-ford.csv` from the backend and renders the audit dashboard (KPIs,
   zone breakdown, unit table, cross-reference upload). Served by the backend at `/`.
3. **Root-level Python analysis scripts** (`pipeline_ford_ml.py`, `clasificar_zonas.py`) —
   ML/analysis pipelines run against `CONSOLIDADO_CON_FORD.csv` directly (not through the
   backend), producing artifacts into `ml_output/` and `zonas_output/`. Independent of the
   backend/dashboard; see `requirements-ml.txt`.

See the root [`README.md`](README.md) for the two-dataset distinction, Docker build, and CI.
Deployed via Railway (`railway.json` at repo root points it at `backend/Dockerfile`);
`CONSOLIDADO_CON_FORD.csv` is baked into the image at build time since it's static
reference data — see `backend/Dockerfile`.

## Backend (`backend/`)

### Commands

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate            # Windows; source .venv/bin/activate on *nix
pip install -r requirements.txt

uvicorn app.main:app --reload     # serve at http://127.0.0.1:8000, docs at /docs

pytest                            # run all tests (24 tests)
pytest tests/test_calc.py         # single file
pytest tests/test_calc.py::test_name -q   # single test

python -m scripts.validate_reference   # re-derive dispositions from the reference CSV
                                        # and diff rollups against acceptance targets
```

### Architecture

```
app/
  config.py       limits, cohort map, precision — single source of truth, env-overridable
  parser.py       raw .txt -> RawFields   (label-based extraction, locale-tolerant numbers)
  calc.py         RawFields -> Unit       (physics + disposition, pure function)
  store.py        SQLite audit store      (idempotent, latest-run-wins, stdlib only)
  consolidate.py  Unit(s) -> CSV / JSON / rollups
  main.py         FastAPI app + routes
```

Key design decisions that shape how to extend this code:

- **Physics/disposition is recomputed on every read, never stored.** `store.py` persists
  only raw parsed fields (`RawFields`); `calc.compute()` derives `Status`, `Razon_Falla`,
  `Ford_220A`, etc. from live `config.LIMITS` on each `iter_units()` call. This means a QE
  threshold retune (env var change) takes effect with no re-ingest — don't be tempted to
  cache or store derived columns.
- **Idempotent ingest keyed on `file_hash`** (sha256 of raw bytes); `(serial, file_hash)`
  is also unique. Retests share a serial — the row with the newest `mtime` is `active`;
  older ones are `superseded` (excluded from dashboard reads, kept for audit trail).
- **`Status` (the 5.700 tester rule) intentionally over-rejects.** It is a three-clause
  rule (`S_High`, `S_Low`, `Ratio_HL`, joined by `|`) reverse-engineered from and verified
  against the reference CSV — see `backend/README.md` for the exact clauses/thresholds.
  `Ford_220A` is the separate, real-world-accurate disposition. **Never collapse the two
  columns** — the whole point of the audit is isolating the ~53 real Ford escapes from the
  ~1,485 units the tester over-rejects.
- **All thresholds live in `config.Limits`**, overridable via env vars
  (`TESTER_MIN_S`, `TESTER_MIN_S_LOW`, `RATIO_MIN`, `FORD_LIMIT_V`, `NOMINAL_RATIO`, ...).
  Never hardcode a threshold literal elsewhere.
- **Cohorts are a closed set** (`config.VALID_COHORTS`): `CORRECTION_FACTOR`,
  `PRODUCCION_SOSPECHOSA`, `SDACS`. Ingest rejects anything else so a folder-name typo
  can't create a phantom cohort.
- **Parser is label-based, not position-based.** `parser.LABELS` locates each value by the
  section label named in the spec (e.g. `CONSUMPTION CURRENT`, `POLARITY` → `VOLTAJE
  C3-C2/C3-C4`) rather than a fixed offset, and tolerates `,`/`.` decimals. If real log
  layout differs from fixtures, adjust `LABELS`, not the extraction logic. Files missing
  any of the four required voltages are quarantined (`data/quarantine/`) and emitted as
  `Status=FAIL, Razon_Falla=parse_error`.
- CSV column order is a contract (`consolidate.py`) — downstream tooling assumes it; see
  `backend/README.md` §3 for the exact header order and numeric precision per column.
- **Two datasets, don't confuse them.** `GET /api/dataset.csv` is derived from the raw-log
  ingest pipeline (has `Categoria`/`Razon_Falla`/`Ratio_Dev_Pct`). `GET
  /api/consolidado-ford.csv` is a **static passthrough** of `CONSOLIDADO_CON_FORD.csv`
  (`config.CONSOLIDADO_FORD_PATH`, default: repo root) — a separate, pre-built dataset with
  `Fecha`/`Anio`/`Mes`/`Dia`, `Margen_Ford_mV`, and `Ford_Real` (Ford's confirmed field
  outcomes; not derivable from a tester log). The dashboard and both ML scripts read the
  latter specifically because it's the only one with ground truth.
- **Auth is opt-in.** `require_api_key` (main.py) gates `POST /api/ingest` and `POST
  /api/crossref` only when `VA_API_KEY` is set; unset (default) disables it. Read-only
  routes are never gated — the browser dashboard has no way to attach the header.
- **The backend serves the dashboard itself**, same-origin, via two explicit routes
  (`GET /` → `index.html`, `GET /support.js`) — never mount `config.FRONTEND_DIR`
  (the repo root) wholesale via `StaticFiles`, that would expose backend source/DB/raw
  logs over HTTP. If you add more static assets, add explicit routes the same way.

Full detail (API table, disposition rules, acceptance targets) is in `backend/README.md` —
read it before making changes to `calc.py` or `parser.py`.

## Root-level dashboard (`index.html`, `support.js`)

- `support.js` is **generated** (`// GENERATED from dc-runtime/src/*.ts — do not edit.
  Rebuild with 'cd dc-runtime && bun run build'`) — a small custom runtime that renders an
  `<x-dc>` template block using React under the hood (`{{ expr }}` interpolation,
  `onClick`/`onInput`/etc. bound to a script's exported handlers). The `dc-runtime` source
  it's built from is not present in this repo — treat `support.js` as a vendored binary and
  make behavioral changes in `index.html`'s inline script instead.
- `index.html` holds the actual dashboard: KPI cards (piezas auditadas, zonas
  rojo/amarillo/verde/limpio per `clasificar_zonas.py`'s thresholds), a searchable/filterable
  unit table, CSV upload (drag-drop), and cross-reference against a shipment file.
- **Now wired to the backend**: `DATASET` (in the component's inline script) fetches
  `/api/consolidado-ford.csv`. Falls back to a small synthetic dataset (`fallback()`) if
  the fetch fails, so the UI still renders with no backend running.
- **Zone-cutoff date logic uses `Anio`/`Mes` columns, not the `Fecha` string.** `Fecha` is
  `M/D/YYYY` (e.g. `6/10/2025`), which does not sort correctly as a string — an earlier
  version compared it lexicographically against an ISO cutoff string, which is wrong (e.g.
  `"6/10/2025" >= "2026-03-01"` is `true` as strings). The fix derives `meskey =
  Anio*100+Mes` and compares against `CORTE_MESKEY=202603`, matching `clasificar_zonas.py`
  exactly. If you touch `parse()`, keep using the numeric columns, not `Fecha`.

## Root-level analysis scripts

- `pipeline_ford_ml.py` — ML pipeline (XGBoost/LightGBM/RandomForest/LogReg + SHAP) trained
  on `CONSOLIDADO_CON_FORD.csv` against the `Ford_Real` label (heavily imbalanced, ~1:511).
  Primary evaluation is `StratifiedGroupKFold` grouped by month to avoid temporal leakage;
  a pure temporal train/test split is reported separately since the test period only
  contains 5 FAIL. Writes plots/metrics to `ml_output/`.
- `clasificar_zonas.py` — a **deterministic business rule**, not a model: classifies every
  unit into a zone (ROJO/AMARILLO/VERDE/LIMPIO) by `S_High_mVA` against fixed thresholds
  and a hard temporal cutover (`CORTE=202603`, March 2026 — when Littelfuse fixed the root
  cause upstream). Validates the rule against `Ford_Real` and specifically reports escapes
  (real FAILs the rule would have released). Runs **both** threshold versions on each
  invocation and writes v1 → `zonas_output/`, v2 → `zonas_output_v2/`:
  - **v1** (`UMBRAL_ROJO=5.55`, `UMBRAL_AMARILLO=5.65`, no rounding) — the historical rule,
    reverse-engineered from our own 462-datum estimate. Still the live-served rule.
  - **v2** (`UMBRAL_ROJO_V2=5.60`, `UMBRAL_AMARILLO_V2=5.65`, `snap_grid=True`) — calibrated
    to Ford's **confirmed** WMA criterion (reject code 12250, `Delta_Crnt > ±2.40 A @ 220 A`
    → `S_MIN_FORD=5.638`, `S_MAX_FORD=5.762`). `ZoneConfig.snap_grid` rounds `S_High` to 4
    decimals before comparing: `S = ΔV/0.020` carries sub-ULP float noise (e.g.
    `0.112/0.020 = 5.600000000000005`), so a threshold on the tester's 0.05-mV/A
    quantization grid (like `5.60`) is otherwise nondeterministic. v2 halves escapes (23→11)
    and lifts zone recall (89.4%→94.9%). Adds a `Ford_Real_Formula` column (Delta_Crnt proxy)
    and writes `comparativa_v1_v2.txt` + `discrepancia_ford_littelfuse.txt`. **Discrepancy
    to remember**: Ford's ±1.09% is tighter than Littelfuse's ±1.7% manufacturing spec —
    parts with S in 5.603–5.638 pass Littelfuse but fail Ford's operating criterion
    (negotiation candidates). v2 is **not yet wired to the dashboard** — validated offline first.
- Both scripts hardcode `BASE`/`CSV`/`OUT` paths relative to their own file location and
  expect `CONSOLIDADO_CON_FORD.csv` at the repo root; run them from anywhere with
  `python pipeline_ford_ml.py` / `python clasificar_zonas.py`.

## ML ranking inside zona AMARILLO (`backend/app/ml_rank.py`)

**This is a ranking aid, not a disposition.** `clasificar_zonas.py`'s deterministic
zone rule (ROJO/AMARILLO/VERDE/LIMPIO → RETIRAR/SORTEO/LIBERAR) is the sole source of
truth for what happens to a unit. `ml_rank.py` only adds `ML_Risk_Score` (P(Ford_Real=
FAIL), 0–1) and `ML_Risk_Rank` (1..N) *within* the ~32K-unit zona AMARILLO pool, so QE
can triage the sorteo list by priority instead of treating it as flat. Outside AMARILLO
both are always `None`/absent — the zone rule already decided those units, ranking
doesn't apply. **Never let `ML_Risk_Score` influence `Status`/`Ford_220A`/the zone
disposition** — it's an orthogonal column, same principle as never collapsing
`Status`/`Ford_220A` into each other.

- **Why no temporal features.** An earlier modeling attempt (`pipeline_ford_ml.py`'s
  exploratory run) used `Dia_Juliano`/`Mes_Cos` as top-SHAP features — the model had
  memorized *which months had failures*, not sensor physics, and its recall collapsed
  outside its training date range. `ml_rank.FEATURES` is physical/electrical only
  (`Consumo_mA`, `Offset_High_V`, `Offset_Low_V`, `V_20A_High_V`, `V_20A_Low_V`,
  `Tiempo_ms_High`, `Tiempo_ms_Low`, `Delta_V_High`, `Delta_V_Low`, `S_High_mVA`,
  `S_Low_mVA`, `Ratio_HL`) — never add `Fecha`/`Anio`/`Mes`/`Dia` or anything derived
  from them. `tests/test_ml_rank.py::test_no_temporal_features` guards this.
- **Zone/dedup logic is duplicated from `clasificar_zonas.py` on purpose** (`ml_rank.
  zona_de`, `CORTE_MESKEY`/`UMBRAL_ROJO`/`UMBRAL_AMARILLO`, `_dedupe_by_serial` — one row
  per serial, earliest/production date wins). Same pattern as `index.html`'s JS
  constants: a business rule maintained in more than one place by necessity. Retune the
  zone thresholds in `clasificar_zonas.py`? Retune them here too. `zona_de`/
  `load_amarillo_rows`/`train_ranking_model` take optional `umbral_rojo`/`umbral_amarillo`/
  `snap_grid` args that **default to v1** — the live serving path (`ml_risk_score_for_row`,
  `compute_ml_rank`) never passes them, so default behavior is byte-identical to before.
- **v2 models (trained by `scripts/train_ml_rank_v2.py`, 3 seeds each).** Two pkls:
  - `models/ml_rank_v2.pkl` (**narrow**) — re-filters AMARILLO with the v2 zone thresholds
    (`5.60 < S ≤ 5.65`, `snap_grid`). **Collapses**: that band is a single DMM-quantized
    value `S=5.65`, so `S_High`/`Delta_V_High` (the dominant features) are constant and the
    ranker has no signal — Gain@25% 84%→14%, ROC-AUC 0.82→**0.38** (worse than chance).
  - `models/ml_rank_v2_wide.pkl` (**wide**) — a two-sided ML-ONLY band
    (`UMBRAL_AMARILLO_ML_MIN=5.638 < S ≤ UMBRAL_AMARILLO_ML_MAX=5.700`, `train_ranking_model(
    band=(...))` / `load_ml_band_rows`) spanning two quantized values (5.65 **and** 5.70).
    Restores signal: ROC-AUC 0.38→**0.56** (above chance), Gain@50% 42%→68% — confirming the
    collapse was quantization, not data scarcity. This band changes **no unit's disposition**
    (the deterministic ROJO/AMARILLO/VERDE rule is untouched); it only widens the pool the
    ranking is *computed over*. `test_wide_ml_band_has_signal` guards the ≥2-value invariant.
  Why v2 still trails v1 on Gain@25%: v2's better zone rule already pulls the 147 strong-signal
  FAILs at `S=5.60` into ROJO (RETIRAR), so the ML only triages the genuinely-hard residual.
  **v1 remains the served model**; both v2 pkls are committed as the record of this finding,
  neither wired to a route until the wide band is confirmed (already stable across 3 seeds).
- **Training is always offline.** `train_ranking_model()` (StratifiedGroupKFold by
  month, candidates RandomForest + a module-level `ScaledLogReg`, picks the better one
  by OOF PR-AUC) is only ever invoked from `scripts/train_ml_rank.py`
  (`python -m scripts.train_ml_rank` from `backend/`), never from a request handler.
  Real numbers on the current `CONSOLIDADO_CON_FORD.csv` (31,949 zona-AMARILLO units
  deduped to one-per-serial, 187 confirmed `Ford_Real=FAIL`): best model `LogReg`,
  PR-AUC 0.031 (≈5.3× the 0.59% base rate), ROC-AUC 0.825, **gain@25%=84.0%** (the top
  25% of the ranking captures 84% of known FAILs). Retrain and recommit
  `backend/models/ml_rank.pkl` + `ml_rank_report.json` whenever the CSV is refreshed —
  they're committed artifacts, not generated at container build time.
- **Serving is batched, not per-row.** `compute_ml_rank()` scores the entire AMARILLO
  matrix in one `predict_proba` call and caches the result in-process (the CSV is
  static). The first implementation looped `predict_proba` per row (~32K individual
  calls) and the endpoint timed out — if you're touching this function, keep the batch
  shape.
- **Endpoints**: `GET /api/ml-rank.csv` (ranked AMARILLO rows: `Serial, Fecha,
  S_High_mVA, Ratio_HL, ML_Risk_Score, ML_Risk_Rank, Ford_Real`) and `GET
  /api/ml-rank/meta` (the training report — what the dashboard's KPI card quotes, so it
  never goes stale relative to the deployed model). Both read-only, no auth, same
  pattern as `/api/consolidado-ford.csv`. 404 if `backend/models/ml_rank.pkl` is
  missing.
- **Dashboard**: `index.html` fetches both endpoints independently in
  `componentDidMount` (best-effort, never blocks the main dataset render). Table gets an
  "ML Risk" badge column (red >50%, orange 20–50%, gray <20%, dash outside AMARILLO) and
  a `mlrank` sort key; the AMARILLO KPI card shows the live gain@25% figure; a new
  "Export Priorizado (Top ML Risk)" button downloads `/api/ml-rank.csv` directly.
