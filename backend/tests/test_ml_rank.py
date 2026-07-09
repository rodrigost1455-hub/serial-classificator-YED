"""Validation for the zona AMARILLO ranking model (app/ml_rank.py).

These tests train against the real CONSOLIDADO_CON_FORD.csv (committed at the
repo root) — there is no synthetic substitute for "does this actually rank
real Ford_Real=FAIL units higher." Training is fast (LogReg/RandomForest on
~32K rows), but test_stability_across_seeds and test_top25_concentration each
do a full OOF training run, so this file runs slower than the rest of the
suite.
"""
import pytest

from app import ml_rank

BANNED_TEMPORAL_FEATURES = {"Fecha", "Anio", "Mes", "Dia", "Dia_Juliano",
                            "Mes_Sin", "Mes_Cos", "MesKey"}


def test_no_temporal_features():
    """The first modeling attempt used Dia_Juliano/Mes_Cos as top-SHAP features
    — pure temporal leakage (Recall=0 outside the training date range). Guard
    against that regressing back in."""
    assert not (set(ml_rank.FEATURES) & BANNED_TEMPORAL_FEATURES)
    assert len(ml_rank.FEATURES) > 0


def test_score_null_outside_amarillo():
    """ML_Risk_Score must be null for ROJO/VERDE/LIMPIO — the zone rule already
    decided those, ranking doesn't apply. This must hold regardless of whether
    a model is trained (zone check happens before scoring)."""
    base = {
        "SerialNumber": "x", "Consumo_mA": "20", "Offset_High_V": "2.5",
        "Offset_Low_V": "2.5", "V_20A_High_V": "2.6", "V_20A_Low_V": "2.9",
        "Tiempo_ms_High": "20", "Tiempo_ms_Low": "19", "Delta_V_High": "0.1",
        "Delta_V_Low": "0.4", "S_Low_mVA": "20", "Ratio_HL": "0.28",
        "Ford_Real": "PASS",
    }
    rojo = {**base, "Fecha": "6/10/2025", "Anio": "2025", "Mes": "6", "S_High_mVA": "5.40"}
    verde = {**base, "Fecha": "6/10/2025", "Anio": "2025", "Mes": "6", "S_High_mVA": "5.80"}
    limpio = {**base, "Fecha": "4/1/2026", "Anio": "2026", "Mes": "4", "S_High_mVA": "5.60"}
    amarillo = {**base, "Fecha": "6/10/2025", "Anio": "2025", "Mes": "6", "S_High_mVA": "5.60"}

    assert ml_rank.ml_risk_score_for_row(rojo) is None
    assert ml_rank.ml_risk_score_for_row(verde) is None
    assert ml_rank.ml_risk_score_for_row(limpio) is None

    score = ml_rank.ml_risk_score_for_row(amarillo)
    assert score is None or 0.0 <= score <= 1.0


def test_zona_boundaries():
    """Sanity-check the thresholds duplicated from clasificar_zonas.py."""
    assert ml_rank.zona_de(202601, 5.55) == "ROJO"
    assert ml_rank.zona_de(202601, 5.551) == "AMARILLO"
    assert ml_rank.zona_de(202601, 5.65) == "AMARILLO"
    assert ml_rank.zona_de(202601, 5.651) == "VERDE"
    assert ml_rank.zona_de(202603, 5.60) == "LIMPIO"
    assert ml_rank.zona_de(202602, None) == "ROJO"


def test_wide_ml_band_has_signal():
    """The whole point of the v2 wide ML band: unlike the narrow v2 AMARILLO
    band (5.60-5.65 → single DMM-quantized S=5.65, no variance for the ranker),
    the wide band (5.638-5.700) must span more than one quantized S_High value,
    or the model has nothing physical to rank on. Guards the collapse from
    silently regressing back if the band constants are retuned."""
    rows = ml_rank.load_ml_band_rows()
    assert rows, "wide ML band selected no rows"
    s_vals = {round(ml_rank._num(r.get("S_High_mVA")), 4) for r in rows
              if ml_rank._num(r.get("S_High_mVA")) is not None}
    assert len(s_vals) >= 2, f"wide band collapsed to {s_vals} — no physical signal"


@pytest.mark.slow
def test_stability_across_seeds():
    """Retraining with a different random_state should give a similar Lift —
    if it doesn't, the model is unstable/overfit to fold composition rather
    than learning real structure."""
    _, r1 = ml_rank.train_ranking_model(random_state=1, n_estimators=200)
    _, r2 = ml_rank.train_ranking_model(random_state=2, n_estimators=200)
    assert abs(r1["gain_top25pct"] - r2["gain_top25pct"]) < 0.10
    assert abs(r1["pr_auc"] - r2["pr_auc"]) < 0.02


@pytest.mark.slow
def test_top25_concentration():
    """The top 25% of the ranking should capture most of zona AMARILLO's known
    Ford_Real=FAIL. Observed on the reference dataset: 84.0% (LogReg, OOF,
    StratifiedGroupKFold by month) — the 60% floor here leaves real margin
    for future dataset drift while still catching a real regression."""
    _, report = ml_rank.train_ranking_model(n_estimators=300)
    assert report["gain_top25pct"] >= 0.60
    assert report["n_fail"] >= 2
