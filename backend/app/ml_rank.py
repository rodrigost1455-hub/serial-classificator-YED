"""ML ranking within the zona AMARILLO (risk/sorteo) pool.

``clasificar_zonas.py`` (repo root) is the sole source of truth for the
deterministic RETIRAR/SORTEO/LIBERAR disposition — this module never touches
that decision. All it does is give QE a *priority order* inside the ~42K-unit
AMARILLO pool, so triage isn't flat: ``ML_Risk_Score`` is P(Ford_Real=FAIL)
from a model trained only on AMARILLO units, and ``ML_Risk_Rank`` is that
pool's units sorted by score, most-suspicious first. Outside AMARILLO the
score is meaningless by construction (the zone rule already decided) and is
always ``None``.

Why no temporal features (Fecha/Anio/Mes/Dia and anything derived from them,
e.g. Dia_Juliano, Mes_Sin/Mes_Cos): the first modeling attempt used those as
top-SHAP features and scored well in-sample — but it had turned into a
lookup table of "which months had failures," not a sensor-physics model, and
its recall on held-out months outside its training range was ~0. Every
feature below is physical/electrical, the same values calc.py derives from a
raw tester log. A model trained this way generalizes to new production
months instead of memorizing old ones.

Training is always offline (``scripts/train_ml_rank.py``, run manually or as
a build step) and serialized to ``backend/models/ml_rank.pkl`` — never
retrained inside a request. ``score_unit``/``ml_risk_score_for_row`` only
ever load and call ``predict_proba`` on the already-trained model.
"""
from __future__ import annotations

import csv
import os
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import config
from .calc import Unit

# Zone thresholds — must match clasificar_zonas.py exactly (same duplication
# pattern already used by index.html's JS constants: these are a business
# rule maintained in more than one place by necessity, not an oversight).
CORTE_MESKEY = 202603
UMBRAL_ROJO = 5.55
UMBRAL_AMARILLO = 5.65

# v2 thresholds — calibrated against Ford's real WMA criterion (Delta_Crnt
# > ±2.40 A @ 220 A → S_MIN_FORD = 5.638 mV/A). Mirrors clasificar_zonas.py's
# UMBRAL_ROJO_V2/UMBRAL_AMARILLO_V2. snap_grid rounds S_High to 4 decimals
# before comparing, because S = ΔV/0.020 carries sub-ULP float noise that would
# otherwise make a threshold sitting exactly on the 0.05 grid (5.60) nondeter-
# ministic — see clasificar_zonas.ZoneConfig. v2 always trains/serves with it on.
UMBRAL_ROJO_V2 = 5.60
UMBRAL_AMARILLO_V2 = 5.65

# v2 "wide" ML band — for the RANKING MODEL ONLY, decoupled from the
# deterministic ROJO/AMARILLO/VERDE disposition (which stays 5.60/5.65). The
# narrow v2 AMARILLO band (5.60 < S ≤ 5.65) collapses to a single DMM-quantized
# value (S=5.65), so S_High/Delta_V_High are constant and the model has no
# physical signal to rank on (Gain@25% 84%→14%, ROC-AUC 0.38). Widening the ML
# band to (5.638, 5.700] spans more than one quantization step (5.65 AND 5.70),
# restoring the variability the model needs. This does NOT change any unit's
# disposition — it only widens the pool the ranking is *computed over*.
#   5.638 = S_MIN_FORD_V2 (Ford's real ±2.40 A operating limit)
#   5.700 = S_NOMINAL (one full quantization step past 5.65)
UMBRAL_AMARILLO_ML_MIN = 5.638
UMBRAL_AMARILLO_ML_MAX = 5.700

# Physical/electrical features only. Never add Fecha/Anio/Mes/Dia or anything
# derived from them here — see module docstring.
FEATURES: List[str] = [
    "Consumo_mA", "Offset_High_V", "Offset_Low_V", "V_20A_High_V", "V_20A_Low_V",
    "Tiempo_ms_High", "Tiempo_ms_Low", "Delta_V_High", "Delta_V_Low",
    "S_High_mVA", "S_Low_mVA", "Ratio_HL",
]

_HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.getenv("VA_ML_RANK_MODEL_PATH",
                       os.path.join(_HERE, "..", "models", "ml_rank.pkl"))


class ScaledLogReg:
    """StandardScaler + LogisticRegression, bundled so it round-trips through
    joblib like a plain sklearn estimator. Module-level (not a closure) so it
    can actually be pickled."""

    def __init__(self, random_state: int = 42):
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        self.sc = StandardScaler()
        self.m = LogisticRegression(max_iter=2000, class_weight="balanced",
                                    random_state=random_state)

    def fit(self, X, y):
        self.m.fit(self.sc.fit_transform(X), y)
        return self

    def predict_proba(self, X):
        return self.m.predict_proba(self.sc.transform(X))


# ------------------------------------------------------------- data loading
def _num(x) -> Optional[float]:
    if x is None:
        return None
    x = str(x).strip().replace(",", ".")
    if x in ("", "None", "nan"):
        return None
    try:
        return float(x)
    except ValueError:
        return None


def _parse_fecha(s: str) -> Tuple[int, int, int]:
    m, d, y = (int(p) for p in s.strip().split("/"))
    return (y, m, d)


def _dedupe_by_serial(rows: List[dict]) -> List[dict]:
    """One row per physical part = its earliest (production) record.

    Mirrors clasificar_zonas.py's dedupe_por_serial exactly: retests must not
    be treated as separate parts, and the zone/period is decided by the
    *production* date, not a later retest date.
    """
    canon: Dict[str, dict] = {}
    fecha_t: Dict[str, tuple] = {}
    for r in rows:
        s = r["SerialNumber"]
        ft = _parse_fecha(r["Fecha"])
        if s not in canon or ft < fecha_t[s]:
            canon[s] = r
            fecha_t[s] = ft
    return list(canon.values())


def zona_de(meskey: int, s_high: Optional[float],
            umbral_rojo: float = UMBRAL_ROJO,
            umbral_amarillo: float = UMBRAL_AMARILLO,
            snap_grid: bool = False) -> str:
    """ROJO/AMARILLO/VERDE/LIMPIO — identical logic to clasificar_zonas.py.

    Defaults reproduce v1 (the live serving model) exactly. Pass the v2
    thresholds + snap_grid=True to filter the v2 zona AMARILLO pool."""
    if meskey >= CORTE_MESKEY:
        return "LIMPIO"
    if s_high is None:
        return "ROJO"
    s = round(s_high, 4) if snap_grid else s_high
    if s <= umbral_rojo:
        return "ROJO"
    if s <= umbral_amarillo:
        return "AMARILLO"
    return "VERDE"


def _row_zona(row: dict, umbral_rojo: float = UMBRAL_ROJO,
              umbral_amarillo: float = UMBRAL_AMARILLO,
              snap_grid: bool = False) -> str:
    meskey = int(row["Anio"]) * 100 + int(row["Mes"])
    return zona_de(meskey, _num(row.get("S_High_mVA")),
                   umbral_rojo, umbral_amarillo, snap_grid)


def load_amarillo_rows(csv_path: Optional[str] = None,
                       umbral_rojo: float = UMBRAL_ROJO,
                       umbral_amarillo: float = UMBRAL_AMARILLO,
                       snap_grid: bool = False) -> List[dict]:
    """Deduped (one-per-serial) rows from CONSOLIDADO_CON_FORD.csv, filtered to
    zona AMARILLO — the only rows the ranking model ever trains or scores on.

    Defaults = v1. Pass v2 thresholds (5.60/5.65, snap_grid=True) for the
    Ford-calibrated pool."""
    path = csv_path or config.CONSOLIDADO_FORD_PATH
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    rows = _dedupe_by_serial(rows)
    return [r for r in rows
            if _row_zona(r, umbral_rojo, umbral_amarillo, snap_grid) == "AMARILLO"]


def _row_in_ml_band(row: dict, s_min: float, s_max: float, snap_grid: bool) -> bool:
    """True if a row falls in the (s_min, s_max] ML band, RIESGO period only.

    Two-sided band for the ranking model, independent of the ROJO/AMARILLO/VERDE
    disposition. LIMPIO (post-March) parts are excluded — same as the zone rule,
    they are clean production and never enter the sorteo."""
    meskey = int(row["Anio"]) * 100 + int(row["Mes"])
    if meskey >= CORTE_MESKEY:
        return False
    s = _num(row.get("S_High_mVA"))
    if s is None:
        return False
    if snap_grid:
        s = round(s, 4)
    return s_min < s <= s_max


def load_ml_band_rows(csv_path: Optional[str] = None,
                      s_min: float = UMBRAL_AMARILLO_ML_MIN,
                      s_max: float = UMBRAL_AMARILLO_ML_MAX,
                      snap_grid: bool = True) -> List[dict]:
    """Deduped rows in the (s_min, s_max] ML band — the widened v2 ranking pool.

    Same one-per-serial dedup and RIESGO-period restriction as
    load_amarillo_rows, but selected by a two-sided sensitivity band instead of
    the ROJO/AMARILLO zone cutoffs, so the pool spans >1 DMM quantization step."""
    path = csv_path or config.CONSOLIDADO_FORD_PATH
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    rows = _dedupe_by_serial(rows)
    return [r for r in rows if _row_in_ml_band(r, s_min, s_max, snap_grid)]


def _feature_vector(row: dict) -> List[float]:
    return [_num(row.get(f)) if _num(row.get(f)) is not None else np.nan for f in FEATURES]


# ---------------------------------------------------------------- training
def train_ranking_model(csv_path: Optional[str] = None, random_state: int = 42,
                        n_estimators: int = 500,
                        umbral_rojo: float = UMBRAL_ROJO,
                        umbral_amarillo: float = UMBRAL_AMARILLO,
                        snap_grid: bool = False,
                        band: Optional[Tuple[float, float]] = None):
    """Train + out-of-fold-validate a ranking model on zona AMARILLO only.

    ``band=(s_min, s_max)`` overrides the zone-based pool with the two-sided ML
    band (load_ml_band_rows) — used by the v2 "wide" model to escape the
    single-quantized-value collapse of the narrow AMARILLO band. When band is
    None the pool is the ROJO/AMARILLO zone selection (v1/narrow-v2 behavior).

    Candidates are RandomForest and a scaled LogisticRegression (both
    sklearn-only, no extra heavy deps beyond what's already required); the
    better one by OOF PR-AUC is refit on all AMARILLO data and returned.

    Validation is StratifiedGroupKFold grouped by month — the same scheme
    pipeline_ford_ml.py uses — specifically to catch temporal leakage: if a
    model only "learned the months," its OOF score collapses once every fold
    holds out different months.

    Returns (model, report). report includes pr_auc, roc_auc, the feature
    list actually used (so tests can assert no temporal leakage), and
    gain_top{10,25,50}pct — the fraction of known Ford_Real=FAIL captured in
    the top N% of the ranking (the number the dashboard KPI quotes).

    Offline-only: never call this from a request handler.
    """
    import pandas as pd
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import average_precision_score, roc_auc_score
    from sklearn.model_selection import StratifiedGroupKFold

    if band is not None:
        rows = load_ml_band_rows(csv_path, band[0], band[1], snap_grid)
    else:
        rows = load_amarillo_rows(csv_path, umbral_rojo, umbral_amarillo, snap_grid)
    if not rows:
        raise ValueError("No ranking pool rows found to train on")
    df = pd.DataFrame(rows)

    for f in FEATURES:
        df[f] = pd.to_numeric(df[f].astype(str).str.replace(",", "."), errors="coerce")
        df[f] = df[f].fillna(df[f].median())

    df["y"] = (df["Ford_Real"].astype(str).str.strip().str.upper() == "FAIL").astype(int)
    df["MesKey"] = df["Anio"].astype(int) * 100 + df["Mes"].astype(int)

    X = df[FEATURES].values
    y = df["y"].values
    groups = df["MesKey"].values
    n_pos = int(y.sum())
    if n_pos < 2:
        raise ValueError(f"Only {n_pos} Ford_Real=FAIL in zona AMARILLO — can't cross-validate")

    n_splits = min(5, n_pos, len(set(groups)))
    n_splits = max(2, n_splits)
    sgkf = StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=random_state)

    def mk_rf():
        return RandomForestClassifier(n_estimators=n_estimators, max_depth=6,
                                       class_weight="balanced", n_jobs=-1,
                                       random_state=random_state)

    candidates = {"RandomForest": mk_rf,
                 "LogReg": lambda: ScaledLogReg(random_state=random_state)}
    best_name, best_oof, best_ap = None, None, -1.0
    for name, ctor in candidates.items():
        oof = np.zeros(len(y))
        for tr, te in sgkf.split(X, y, groups):
            m = ctor()
            m.fit(X[tr], y[tr])
            oof[te] = m.predict_proba(X[te])[:, 1]
        ap = average_precision_score(y, oof)
        if ap > best_ap:
            best_name, best_oof, best_ap = name, oof, ap

    report = {
        "model": best_name,
        "n": int(len(y)),
        "n_fail": n_pos,
        "pr_auc": float(best_ap),
        "roc_auc": float(roc_auc_score(y, best_oof)),
        "features": list(FEATURES),
        "zone": {"umbral_rojo": umbral_rojo, "umbral_amarillo": umbral_amarillo,
                 "snap_grid": snap_grid,
                 "band": list(band) if band is not None else None},
        **_lift_report(y, best_oof),
    }

    final_model = candidates[best_name]()
    final_model.fit(X, y)
    return final_model, report


def _lift_report(y: np.ndarray, proba: np.ndarray) -> Dict[str, float]:
    """gain_topNpct = fraction of all known FAILs captured in the top N% of
    the ranking (sorted by proba descending)."""
    order = np.argsort(-proba)
    y_sorted = y[order]
    total_fail = int(y.sum())
    out: Dict[str, float] = {}
    for pct in (10, 25, 50):
        k = max(1, int(round(len(y) * pct / 100)))
        captured = int(y_sorted[:k].sum())
        out[f"gain_top{pct}pct"] = (captured / total_fail) if total_fail else 0.0
    return out


# -------------------------------------------------------- serialize / load
def save_model(model, report: dict, path: Optional[str] = None) -> str:
    import json

    import joblib

    path = path or MODEL_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    joblib.dump({"model": model, "features": FEATURES}, path)
    report_path = os.path.splitext(path)[0] + "_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return path


_MODEL_CACHE: Optional[dict] = None
_REPORT_CACHE: Optional[dict] = None


def _load_model() -> Optional[dict]:
    global _MODEL_CACHE
    if _MODEL_CACHE is None:
        if not os.path.exists(MODEL_PATH):
            return None
        import joblib
        _MODEL_CACHE = joblib.load(MODEL_PATH)
    return _MODEL_CACHE


def is_model_trained() -> bool:
    """Whether a trained model is available at MODEL_PATH — callers (e.g. the
    /api/ml-rank.csv route) use this instead of reaching into _load_model."""
    return _load_model() is not None


def load_report() -> Optional[dict]:
    """The training report saved alongside the model — what the dashboard KPI
    and /api/ml-rank/meta quote. None if the model hasn't been trained yet."""
    global _REPORT_CACHE
    if _REPORT_CACHE is None:
        report_path = os.path.splitext(MODEL_PATH)[0] + "_report.json"
        if not os.path.exists(report_path):
            return None
        import json
        with open(report_path, encoding="utf-8") as f:
            _REPORT_CACHE = json.load(f)
    return _REPORT_CACHE


# -------------------------------------------------------------- inference
def _score_features(feat_dict: Dict[str, Optional[float]]) -> Optional[float]:
    bundle = _load_model()
    if bundle is None:
        return None
    model, feats = bundle["model"], bundle["features"]
    x = np.array([[feat_dict.get(f, np.nan) if feat_dict.get(f) is not None else np.nan
                   for f in feats]])
    return float(model.predict_proba(x)[:, 1][0])


def score_unit(unit: Unit) -> Optional[float]:
    """P(Ford_Real=FAIL) for one raw-log-derived Unit (calc.py). Only
    meaningful in zona AMARILLO — callers must check zone membership
    themselves (this function scores unconditionally; it has no Fecha/Anio
    to determine zone from a bare Unit). Returns None if no model is trained
    yet."""
    feats = {
        "Consumo_mA": unit.raw.Consumo_mA,
        "Offset_High_V": unit.raw.Offset_High_V,
        "Offset_Low_V": unit.raw.Offset_Low_V,
        "V_20A_High_V": unit.raw.V_20A_High_V,
        "V_20A_Low_V": unit.raw.V_20A_Low_V,
        "Tiempo_ms_High": unit.raw.Tiempo_ms_High,
        "Tiempo_ms_Low": unit.raw.Tiempo_ms_Low,
        "Delta_V_High": unit.Delta_V_High,
        "Delta_V_Low": unit.Delta_V_Low,
        "S_High_mVA": unit.S_High_mVA,
        "S_Low_mVA": unit.S_Low_mVA,
        "Ratio_HL": unit.Ratio_HL,
    }
    return _score_features(feats)


def ml_risk_score_for_row(row: dict) -> Optional[float]:
    """P(Ford_Real=FAIL) for one CONSOLIDADO_CON_FORD.csv row — None outside
    zona AMARILLO (ROJO/VERDE already have a determined disposition; LIMPIO
    is post-correction) or if no model is trained yet."""
    if _row_zona(row) != "AMARILLO":
        return None
    feats = {f: _num(row.get(f)) for f in FEATURES}
    return _score_features(feats)


# --------------------------------------------------------------- endpoint
_RANK_CACHE: Optional[List[dict]] = None


def compute_ml_rank(csv_path: Optional[str] = None, use_cache: bool = True) -> List[dict]:
    """Zona AMARILLO rows scored + ranked, most-suspicious first. Each dict:
    Serial, Fecha, S_High_mVA, Ratio_HL, ML_Risk_Score, ML_Risk_Rank, Ford_Real.

    Scores the whole AMARILLO matrix in one batched predict_proba call — not
    one call per row, which is what made the endpoint time out at ~32K rows
    the first time this was written. CONSOLIDADO_CON_FORD.csv is static
    (baked into the image), so the result is cached after the first call;
    pass use_cache=False (tests, or a custom csv_path) to bypass that.
    """
    global _RANK_CACHE
    if use_cache and csv_path is None and _RANK_CACHE is not None:
        return _RANK_CACHE

    bundle = _load_model()
    rows = load_amarillo_rows(csv_path)
    if bundle is None or not rows:
        return []

    model, feats = bundle["model"], bundle["features"]
    X = np.array([[_num(r.get(f)) if _num(r.get(f)) is not None else np.nan for f in feats]
                 for r in rows])
    scores = model.predict_proba(X)[:, 1]

    scored = [{
        "Serial": r["SerialNumber"],
        "Fecha": r["Fecha"],
        "S_High_mVA": r.get("S_High_mVA", ""),
        "Ratio_HL": r.get("Ratio_HL", ""),
        "ML_Risk_Score": round(float(s), 6),
        "Ford_Real": r.get("Ford_Real", ""),
    } for r, s in zip(rows, scores)]
    scored.sort(key=lambda d: d["ML_Risk_Score"], reverse=True)
    for i, d in enumerate(scored, start=1):
        d["ML_Risk_Rank"] = i

    if use_cache and csv_path is None:
        _RANK_CACHE = scored
    return scored


ML_RANK_COLUMNS = ["Serial", "Fecha", "S_High_mVA", "Ratio_HL",
                   "ML_Risk_Score", "ML_Risk_Rank", "Ford_Real"]


def ml_rank_csv_string(csv_path: Optional[str] = None) -> str:
    import io

    rows = compute_ml_rank(csv_path)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(ML_RANK_COLUMNS)
    for r in rows:
        w.writerow([r[c] for c in ML_RANK_COLUMNS])
    return buf.getvalue()
