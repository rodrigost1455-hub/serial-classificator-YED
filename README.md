# Virtual Audit — BEC Current Sensor (Ford PHEV Gen-4)

Yazaki YED's audit tool for the Ford BEC current-sensor field-failure recall. Ford
rejected units that **passed** our 20 A end-of-line (EOL) tester; root cause is a
sensitivity (slope) defect invisible at 20 A but out of tolerance at Ford's 220 A
operating point. Everything in this repo turns on one formula:

```
S = ΔV / 0.020 A                         # slope, mV/A
Proy_220A = offset_mV + S · 220          # projected voltage at Ford's 220 A
```

## Layout

```
backend/                    FastAPI service — ingests raw EOL .txt logs, serves the API
index.html, support.js      Dashboard — fetches data from the backend, renders KPIs/zones/table
pipeline_ford_ml.py         ML pipeline (XGBoost/LightGBM/RF/LogReg + SHAP) — Ford_Real target
clasificar_zonas.py         Deterministic zone-classification rule (not ML) — validates against Ford_Real
CONSOLIDADO_CON_FORD.csv    The dataset both the dashboard and the ML scripts read (see below)
```

## The two datasets — don't confuse them

| File | Produced by | Has |
|---|---|---|
| `Dataset_CONSOLIDADO.csv` (`GET /api/dataset.csv`) | Backend's raw-log ingest pipeline (`backend/app/parser.py` → `calc.py`) | `Categoria`, `Razon_Falla`, `Ratio_Dev_Pct`, our own `Status`/`Ford_220A` disposition |
| `CONSOLIDADO_CON_FORD.csv` (`GET /api/consolidado-ford.csv`) | A standalone, pre-built dataset (not derived from ingest) | `Fecha`/`Anio`/`Mes`/`Dia`, `Margen_Ford_mV`, and **`Ford_Real`** — Ford's confirmed field-failure ground truth |

`Ford_Real` can't be computed from a tester log — it's Ford's actual field data. The
dashboard and both ML scripts read `CONSOLIDADO_CON_FORD.csv` specifically because
it's the only dataset with real outcomes to train/validate against. The backend
serves it as a read-only passthrough (`main.py:consolidado_ford_csv`) rather than
folding it into the ingest pipeline.

## Quick start

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate            # Windows; source .venv/bin/activate on *nix
pip install -r requirements.txt

uvicorn app.main:app --reload     # http://127.0.0.1:8000
```

Then open **http://127.0.0.1:8000/** — the backend serves the dashboard itself
(same-origin, so no CORS setup needed) and the dashboard fetches
`/api/consolidado-ford.csv` on load. If that file is missing or unreachable, the
dashboard falls back to a small synthetic dataset so the UI still renders — drag a
CSV onto it to load real data manually.

Point `VA_CONSOLIDADO_FORD_PATH` at the file if it doesn't live at the repo root
(defaults to `CONSOLIDADO_CON_FORD.csv` next to this README).

Full backend API/architecture reference: [`backend/README.md`](backend/README.md).

### Running the ML / zone-classification scripts

```bash
python -m venv .venv-ml
.venv-ml/Scripts/activate
pip install -r requirements-ml.txt

python pipeline_ford_ml.py        # -> ml_output/   (models, SHAP, plots)
python clasificar_zonas.py        # -> zonas_output/ (zone classification + escape report)
```

Both read `CONSOLIDADO_CON_FORD.csv` from the repo root and are independent of the
backend/dashboard — run them any time the dataset is refreshed.

## Deploying with Docker

Build from the **repo root** (the image needs both `backend/` and the root-level
`index.html`/`support.js` in its build context):

```bash
docker build -f backend/Dockerfile -t virtual-audit-backend .
docker run -p 8000:8000 \
  -v "$(pwd)/CONSOLIDADO_CON_FORD.csv:/data/CONSOLIDADO_CON_FORD.csv:ro" \
  -e VA_CONSOLIDADO_FORD_PATH=/data/CONSOLIDADO_CON_FORD.csv \
  -e VA_API_KEY=change-me \
  virtual-audit-backend
```

`VA_API_KEY` gates the mutating routes (`POST /api/ingest`, `POST /api/crossref`) —
unset by default (fine for local/dev), set it before exposing the service beyond
localhost. Read-only routes (the CSV/summary/units endpoints, the dashboard itself)
stay open since the browser dashboard has no way to attach the header. See
`backend/app/config.py` for the rest of the env-var surface (`VA_CORS_ORIGINS`,
`VA_DATA_DIR`, thresholds, etc).

CI (`.github/workflows/backend-tests.yml`) runs `pytest` in `backend/` on every push/PR.
