# -*- coding: utf-8 -*-
"""
Pipeline ML — Detección de sensores Hall CH1S010B con núcleo desalineado
Yazaki YED / Ford BEC — 113K piezas, target Ford_Real (1:511)

Evaluación principal: StratifiedGroupKFold (k=5, grupo=Mes) — sin fuga temporal
entre folds. El split temporal puro (train jun25-mar26 / test abr26-jul26) se
reporta aparte porque el test solo contiene 5 FAIL.
"""
import os
import sys
import warnings
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns

from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.metrics import (precision_score, recall_score, f1_score,
                             average_precision_score, roc_auc_score,
                             confusion_matrix, precision_recall_curve, roc_curve)
import xgboost as xgb
import lightgbm as lgb
import shap

warnings.filterwarnings("ignore")
RNG = 42
np.random.seed(RNG)

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "ml_output")
os.makedirs(OUT, exist_ok=True)
CSV = os.path.join(BASE, "CONSOLIDADO_CON_FORD.csv")

FORD_LIMIT_MV = 3746.0

def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)

# ============================================================== 1. DATA PREP
hr("1. DATA PREPARATION")
df = pd.read_csv(CSV, dtype={"SerialNumber": str}, low_memory=False)
n0 = len(df)
print(f"Filas cargadas: {n0:,}")

# Limpieza: NG del tester no embarca; duplicados de serial = retests (se
# conserva el último registro por fecha)
df = df[df["Estatus_Tester"] == "OK"].copy()
print(f"Excluidas NG tester: {n0 - len(df)}")
df["Fecha"] = pd.to_datetime(df["Fecha"])
df = df.sort_values("Fecha").drop_duplicates("SerialNumber", keep="last")
print(f"Tras dedupe seriales (retests): {len(df):,}")

# Sensor muerto
dead = (df["S_High_mVA"] == 0) | df["S_High_mVA"].isna()
print(f"Excluidas S_High==0 / NaN (sensor muerto): {dead.sum()}")
df = df[~dead].copy()

df["y"] = (df["Ford_Real"] == "FAIL").astype(int)

# ---- Features adicionales
df["S_Dev_High_pct"] = (df["S_High_mVA"] - 5.77) / 5.77 * 100
df["S_Dev_Low_pct"]  = (df["S_Low_mVA"] - 20.0) / 20.0 * 100
df["Ratio_Dev_pct"]  = (df["Ratio_HL"] - 0.2885) / 0.2885 * 100
df["Consumo_x_S"]    = df["Consumo_mA"] * df["S_High_mVA"]
df["Delta_T_ms"]     = df["Tiempo_ms_High"] - df["Tiempo_ms_Low"]
df["Offset_Diff"]    = df["Offset_High_V"] - df["Offset_Low_V"]
df["Margen_abs"]     = df["Margen_Ford_mV"].abs()
# Dia_Juliano con día-del-año (Anio*1000+Dia del mes no captura drift continuo)
df["Dia_Juliano"]    = df["Anio"] * 1000 + df["Fecha"].dt.dayofyear
df["Mes_Sin"]        = np.sin(2 * np.pi * df["Mes"] / 12)
df["Mes_Cos"]        = np.cos(2 * np.pi * df["Mes"] / 12)
df["T_High_bin"]     = (df["Tiempo_ms_High"] > 25).astype(int)
df["MesKey"]         = df["Anio"] * 100 + df["Mes"]   # grupo para CV

print(f"\nDistribución de clases: FAIL={df['y'].sum()}  PASS={(1-df['y']).sum():,}"
      f"  ratio 1:{int((1-df['y']).sum()/df['y'].sum())}")
print("\nFAIL por mes:")
tab_mes = df.groupby("MesKey").agg(n=("y", "size"), FAIL=("y", "sum"))
tab_mes["FAIL_pct"] = (tab_mes["FAIL"] / tab_mes["n"] * 100).round(3)
print(tab_mes.to_string())

# ---- Feature sets: SIN (limpio) y CON features derivados de la regla baseline
FEATS_BASE = ["Consumo_mA", "Offset_High_V", "Offset_Low_V", "V_20A_High_V",
              "V_20A_Low_V", "Tiempo_ms_High", "Tiempo_ms_Low", "Delta_V_High",
              "Delta_V_Low", "S_High_mVA", "S_Low_mVA", "Ratio_HL",
              "S_Dev_High_pct", "S_Dev_Low_pct", "Ratio_Dev_pct", "Consumo_x_S",
              "Delta_T_ms", "Offset_Diff", "Dia_Juliano", "Mes_Sin", "Mes_Cos",
              "T_High_bin"]
FEATS_LEAK = FEATS_BASE + ["Proy_220A_High_mV", "Proy_220A_Low_mV",
                           "Margen_Ford_mV", "Margen_abs"]
# Ford_220A se excluye SIEMPRE (es la clasificación de la regla, no una medición)

# Imputación simple de NaNs residuales (mediana)
for c in set(FEATS_LEAK):
    if df[c].isna().any():
        df[c] = df[c].fillna(df[c].median())
        print(f"  NaN imputado con mediana: {c}")

y = df["y"].values
groups = df["MesKey"].values

# ============================================================== 2. EDA
hr("2. EXPLORATORY DATA ANALYSIS")

# Separación |dmu|/sigma por feature
rows = []
for c in FEATS_LEAK:
    mu_f, mu_p = df.loc[df.y == 1, c].mean(), df.loc[df.y == 0, c].mean()
    sd = df[c].std()
    rows.append((c, abs(mu_f - mu_p) / sd if sd > 0 else 0, mu_f, mu_p))
sep = pd.DataFrame(rows, columns=["feature", "sep", "mu_FAIL", "mu_PASS"]) \
        .sort_values("sep", ascending=False)
print("\nSeparación |Δμ|/σ (top 15):")
print(sep.head(15).to_string(index=False, float_format=lambda x: f"{x:.4f}"))

fig, axes = plt.subplots(2, 2, figsize=(15, 11))
ax = axes[0, 0]
bins = np.arange(5.3, 6.0, 0.05)
ax.hist(df.loc[df.y == 0, "S_High_mVA"], bins=bins, alpha=.6, label="PASS",
        density=True, color="tab:green")
ax.hist(df.loc[df.y == 1, "S_High_mVA"], bins=bins, alpha=.6, label="FAIL",
        density=True, color="tab:red")
ax.axvspan(5.55, 5.65, color="gold", alpha=.25, label="zona gris")
ax.set_xlabel("S_High_mVA"); ax.set_ylabel("densidad")
ax.set_title("S_High: FAIL vs PASS"); ax.legend()

ax = axes[0, 1]
top10 = sep.head(10)
ax.barh(top10.feature[::-1], top10.sep[::-1], color="tab:blue")
ax.set_xlabel("|Δμ|/σ"); ax.set_title("Top 10 features por separación")

ax = axes[1, 0]
tab_mes_plot = tab_mes.copy()
tab_mes_plot.index = tab_mes_plot.index.astype(str)
ax.bar(tab_mes_plot.index, tab_mes_plot["FAIL"], color="tab:red")
ax.set_title("FAIL por mes"); ax.tick_params(axis="x", rotation=60)

ax = axes[1, 1]
samp = pd.concat([df[df.y == 0].sample(min(8000, (df.y == 0).sum()), random_state=RNG),
                  df[df.y == 1]])
ax.scatter(samp.loc[samp.y == 0, "S_High_mVA"], samp.loc[samp.y == 0, "Offset_High_V"],
           s=4, alpha=.3, c="tab:green", label="PASS")
ax.scatter(samp.loc[samp.y == 1, "S_High_mVA"], samp.loc[samp.y == 1, "Offset_High_V"],
           s=14, alpha=.85, c="tab:red", label="FAIL")
ax.set_xlabel("S_High_mVA"); ax.set_ylabel("Offset_High_V")
ax.set_title("S_High vs Offset_High"); ax.legend()
plt.tight_layout()
plt.savefig(os.path.join(OUT, "eda_overview.png"), dpi=130); plt.close()

plt.figure(figsize=(13, 10))
sns.heatmap(df[FEATS_LEAK].corr(), cmap="coolwarm", center=0,
            annot=False, square=True)
plt.title("Correlation heatmap")
plt.tight_layout()
plt.savefig(os.path.join(OUT, "correlation_heatmap.png"), dpi=130); plt.close()
print("Guardado: eda_overview.png, correlation_heatmap.png")

# ============================================================== 3. SPLIT
hr("3. TRAIN/TEST SPLIT")
train_mask = df["MesKey"] <= 202603
test_mask = ~train_mask
print(f"Split temporal — train jun25-mar26: {train_mask.sum():,} pzas "
      f"({y[train_mask.values].sum()} FAIL) | test abr26-jul26: "
      f"{test_mask.sum():,} pzas ({y[test_mask.values].sum()} FAIL)")
print("El test temporal tiene <10 FAIL → métricas puntuales no confiables.")
print("Evaluación principal: StratifiedGroupKFold k=5, grupo=Mes (OOF).")

sgkf = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=RNG)
folds = list(sgkf.split(df, y, groups))
for i, (tr, te) in enumerate(folds):
    print(f"  Fold {i}: test meses={sorted(set(groups[te]))}  "
          f"FAIL test={y[te].sum()}")

# ============================================================== helpers
def metrics_at(y_true, proba, thr):
    pred = (proba >= thr).astype(int)
    return dict(recall=recall_score(y_true, pred, zero_division=0),
                precision=precision_score(y_true, pred, zero_division=0),
                f1=f1_score(y_true, pred, zero_division=0))

def tune_threshold(y_true, proba, min_recall=0.90):
    """Menor threshold con recall>=min_recall (max precision bajo esa
    restricción) + threshold de F1 máximo."""
    prec, rec, thr = precision_recall_curve(y_true, proba)
    # precision_recall_curve: len(thr) = len(prec)-1
    f1s = 2 * prec * rec / np.clip(prec + rec, 1e-12, None)
    ok = np.where(rec[:-1] >= min_recall)[0]
    thr_rec = thr[ok[-1]] if len(ok) else thr[0]
    thr_f1 = thr[np.nanargmax(f1s[:-1])]
    return thr_rec, thr_f1

def oof_predict(model_fn, X, y, folds, undersample=None):
    """OOF proba con StratifiedGroupKFold. undersample = ratio PASS:FAIL en
    train (e.g. 10) o None."""
    oof = np.zeros(len(y))
    for tr, te in folds:
        Xtr, ytr = X[tr], y[tr]
        if undersample:
            pos = np.where(ytr == 1)[0]
            neg = np.where(ytr == 0)[0]
            keep_n = min(len(neg), undersample * len(pos))
            neg_keep = np.random.RandomState(RNG).choice(neg, keep_n, replace=False)
            idx = np.concatenate([pos, neg_keep])
            Xtr, ytr = Xtr[idx], ytr[idx]
        m = model_fn(ytr)
        m.fit(Xtr, ytr)
        oof[te] = m.predict_proba(X[te])[:, 1]
    return oof

def summarize(name, y_true, proba, min_recall=0.90):
    ap = average_precision_score(y_true, proba)
    roc = roc_auc_score(y_true, proba)
    thr_rec, thr_f1 = tune_threshold(y_true, proba, min_recall)
    m_rec = metrics_at(y_true, proba, thr_rec)
    m_f1 = metrics_at(y_true, proba, thr_f1)
    print(f"{name:38s} PR-AUC={ap:.4f} ROC-AUC={roc:.4f} | "
          f"R>=90%: thr={thr_rec:.4f} R={m_rec['recall']:.3f} "
          f"P={m_rec['precision']:.4f} | bestF1: thr={thr_f1:.4f} "
          f"F1={m_f1['f1']:.3f} (R={m_f1['recall']:.3f} P={m_f1['precision']:.3f})")
    return dict(name=name, pr_auc=ap, roc_auc=roc,
                thr_rec=thr_rec, **{f"rec90_{k}": v for k, v in m_rec.items()},
                thr_f1=thr_f1, **{f"f1_{k}": v for k, v in m_f1.items()})

spw = (y == 0).sum() / (y == 1).sum()

def mk_xgb(spw_val):
    return lambda ytr: xgb.XGBClassifier(
        n_estimators=500, max_depth=4, learning_rate=0.05,
        scale_pos_weight=spw_val, subsample=0.9, colsample_bytree=0.9,
        eval_metric="aucpr", n_jobs=-1, random_state=RNG, tree_method="hist")

def mk_lgbm(spw_val):
    return lambda ytr: lgb.LGBMClassifier(
        n_estimators=500, max_depth=4, learning_rate=0.05,
        scale_pos_weight=spw_val, subsample=0.9, colsample_bytree=0.9,
        n_jobs=-1, random_state=RNG, verbose=-1)

def mk_rf():
    return lambda ytr: RandomForestClassifier(
        n_estimators=500, max_depth=6, class_weight="balanced",
        n_jobs=-1, random_state=RNG)

class ScaledLogReg:
    def __init__(self):
        self.sc = StandardScaler()
        self.m = LogisticRegression(max_iter=2000, class_weight="balanced")
    def fit(self, X, y):
        self.m.fit(self.sc.fit_transform(X), y); return self
    def predict_proba(self, X):
        return self.m.predict_proba(self.sc.transform(X))

# ============================================================== 4. DESBALANCE
hr("4. ESTRATEGIAS DE DESBALANCE (XGBoost, features SIN leak)")
Xb = df[FEATS_BASE].values
res_str = []
res_str.append(summarize("a) XGB scale_pos_weight=511",
               y, oof_predict(mk_xgb(spw), Xb, y, folds)))
res_str.append(summarize("b) XGB undersample 1:10 + spw=10",
               y, oof_predict(mk_xgb(10), Xb, y, folds, undersample=10)))
res_str.append(summarize("c) RF class_weight=balanced",
               y, oof_predict(mk_rf(), Xb, y, folds)))
best_strategy = max(res_str, key=lambda r: r["pr_auc"])
print(f"\nMejor estrategia por PR-AUC: {best_strategy['name']}")

# ============================================================== 5. MODELOS
hr("5. MODEL TRAINING — CV agrupada por mes, OOF")
model_defs = {
    "LogReg":   (lambda ytr: ScaledLogReg(), None),
    "RandomForest": (mk_rf(), None),
    "XGBoost":  (mk_xgb(spw), None),
    "LightGBM": (mk_lgbm(spw), None),
}
results, oofs = {}, {}
for fs_name, feats in [("SIN_leak", FEATS_BASE), ("CON_leak", FEATS_LEAK)]:
    print(f"\n--- Feature set: {fs_name} ({len(feats)} features) ---")
    X = df[feats].values
    for mname, (fn, us) in model_defs.items():
        proba = oof_predict(fn, X, y, folds, undersample=us)
        key = f"{mname}_{fs_name}"
        oofs[key] = proba
        results[key] = summarize(key, y, proba)

res_df = pd.DataFrame(results).T.sort_values("pr_auc", ascending=False)
best_key = res_df.index[0]
best_feats = FEATS_LEAK if best_key.endswith("CON_leak") else FEATS_BASE
best_oof = oofs[best_key]
best_thr = results[best_key]["thr_rec"]
print(f"\n>>> MEJOR MODELO: {best_key} (PR-AUC={res_df.iloc[0].pr_auc:.4f})")
print(f">>> Threshold operativo (recall>=90% OOF): {best_thr:.4f}")

# ============================================================== 6. BASELINE
hr("6. BASELINE: Proy_220A_High_mV < 3746 -> FAIL")
base_pred = (df["Proy_220A_High_mV"] < FORD_LIMIT_MV).astype(int).values
cm = confusion_matrix(y, base_pred)
bR = recall_score(y, base_pred); bP = precision_score(y, base_pred)
bF = f1_score(y, base_pred)
print(f"Confusion matrix:\n{cm}")
print(f"Recall={bR:.3f}  Precision={bP:.4f}  F1={bF:.3f}")
ml_pred = (best_oof >= best_thr).astype(int)
mR = recall_score(y, ml_pred); mP = precision_score(y, ml_pred)
mF = f1_score(y, ml_pred)
print(f"\n{'':22s}{'Recall':>8s}{'Precision':>11s}{'F1':>8s}")
print(f"{'Regla Proy<3746':22s}{bR:8.3f}{bP:11.4f}{bF:8.3f}")
print(f"{best_key:22s}{mR:8.3f}{mP:11.4f}{mF:8.3f}")
supera = "SI" if (mF > bF and mR >= bR - 0.02) else "NO"
print(f"\n¿El ML supera la regla? {supera}")

# ============================================================== 7. IMPORTANCE
hr("7. FEATURE IMPORTANCE + SHAP (mejor modelo, fit en todo el dataset)")
Xbest = df[best_feats].values
if "XGBoost" in best_key:
    final_model = mk_xgb(spw)(y)
elif "LightGBM" in best_key:
    final_model = mk_lgbm(spw)(y)
elif "RandomForest" in best_key:
    final_model = mk_rf()(y)
else:
    final_model = mk_xgb(spw)(y)   # SHAP tree sobre XGB de respaldo
final_model.fit(Xbest, y)

# gain importance
if hasattr(final_model, "feature_importances_"):
    imp = pd.Series(final_model.feature_importances_, index=best_feats) \
            .sort_values(ascending=False)
    plt.figure(figsize=(9, 7))
    imp.head(15)[::-1].plot.barh(color="tab:blue")
    plt.title(f"Feature importance (gain) — {best_key}")
    plt.tight_layout()
    plt.savefig(os.path.join(OUT, "feature_importance.png"), dpi=130); plt.close()
    print("Top 10 por importancia:")
    print(imp.head(10).to_string(float_format=lambda v: f"{v:.4f}"))

# SHAP en muestra (todos los FAIL + 6000 PASS)
idx_shap = np.concatenate([np.where(y == 1)[0],
                           np.random.RandomState(RNG).choice(
                               np.where(y == 0)[0], 6000, replace=False)])
Xs = pd.DataFrame(Xbest[idx_shap], columns=best_feats)
explainer = shap.TreeExplainer(final_model)
sv = explainer.shap_values(Xs)
if isinstance(sv, list):
    sv = sv[1]
elif isinstance(sv, np.ndarray) and sv.ndim == 3:
    sv = sv[:, :, 1]   # (n_samples, n_features, n_classes) -> clase FAIL
plt.figure()
shap.summary_plot(sv, Xs, show=False, max_display=15)
plt.tight_layout()
plt.savefig(os.path.join(OUT, "shap_summary.png"), dpi=130); plt.close()

mean_abs = np.abs(sv).mean(0)
top3 = [best_feats[i] for i in np.argsort(mean_abs)[::-1][:3]]
fig, axes = plt.subplots(1, 3, figsize=(18, 5))
for axi, feat in zip(axes, top3):
    j = best_feats.index(feat)
    axi.scatter(Xs[feat], sv[:, j], s=5, alpha=.4, c="tab:blue")
    axi.set_xlabel(feat); axi.set_ylabel("SHAP")
    axi.set_title(feat)
plt.tight_layout()
plt.savefig(os.path.join(OUT, "shap_dependence_top3.png"), dpi=130); plt.close()
print(f"Top 3 SHAP: {top3}")
print("Guardado: feature_importance.png, shap_summary.png, shap_dependence_top3.png")

# ============================================================== 8. ZONAS
hr("8. ZONE-BASED HYBRID")
zona = pd.cut(df["S_High_mVA"], [-np.inf, 5.55, 5.65, np.inf],
              labels=["ROJO", "AMARILLO", "VERDE"])
print(pd.crosstab(zona, df["Ford_Real"]))
mask_am = (zona == "AMARILLO").values
if mask_am.sum() and y[mask_am].sum():
    ap_am = average_precision_score(y[mask_am], best_oof[mask_am])
    thr_am, _ = tune_threshold(y[mask_am], best_oof[mask_am], 0.90)
    m_am = metrics_at(y[mask_am], best_oof[mask_am], thr_am)
    prev_am = y[mask_am].mean()
    print(f"\nZona AMARILLA: n={mask_am.sum():,}  FAIL={y[mask_am].sum()} "
          f"(prevalencia={prev_am:.4f})")
    print(f"ML en amarilla: PR-AUC={ap_am:.4f} (azar={prev_am:.4f}) | "
          f"R>=90%: R={m_am['recall']:.3f} P={m_am['precision']:.4f} "
          f"F1={m_am['f1']:.3f}")
    print(f"Valor agregado del ML en zona gris: "
          f"{'SI' if ap_am > 3 * prev_am else 'MARGINAL/NO'} "
          f"(PR-AUC {ap_am/prev_am:.1f}x sobre azar)")
# disposición híbrida global
disp = np.where(df["S_High_mVA"] <= 5.55, 1,
        np.where(df["S_High_mVA"] > 5.65, 0, (best_oof >= best_thr).astype(int)))
hR = recall_score(y, disp); hP = precision_score(y, disp); hF = f1_score(y, disp)
print(f"\nHíbrido global: Recall={hR:.3f} Precision={hP:.4f} F1={hF:.3f}")

fig, ax = plt.subplots(figsize=(9, 5))
zc = pd.crosstab(zona, df["Ford_Real"])
zc.plot.bar(ax=ax, color={"FAIL": "tab:red", "PASS": "tab:green"}, logy=True)
ax.set_title("Piezas por zona (log)"); ax.set_ylabel("piezas")
plt.tight_layout()
plt.savefig(os.path.join(OUT, "zone_analysis.png"), dpi=130); plt.close()

# ============================================================== 9. TEMPORAL
hr("9. ANÁLISIS TEMPORAL")
df["_pred"] = ml_pred
df["_proba"] = best_oof
rec_mes = df[df.y == 1].groupby("MesKey")["_pred"].mean()
print("Recall OOF por mes (solo meses con FAIL):")
print(rec_mes.to_string(float_format=lambda v: f"{v:.3f}"))

plt.figure(figsize=(10, 5))
plt.plot(rec_mes.index.astype(str), rec_mes.values, "o-", color="tab:red")
plt.axhline(0.90, ls="--", c="gray", label="objetivo 90%")
plt.title(f"Recall por mes — {best_key}"); plt.ylabel("recall")
plt.xticks(rotation=60); plt.legend(); plt.tight_layout()
plt.savefig(os.path.join(OUT, "temporal_analysis.png"), dpi=130); plt.close()

# Generalización: entrenar oct-dic 2025 → probar jun 2026
tr_m = df["MesKey"].isin([202510, 202511, 202512]).values
te_m = (df["MesKey"] == 202606).values
if y[te_m].sum():
    m = mk_xgb((y[tr_m] == 0).sum() / max(1, y[tr_m].sum()))(y[tr_m])
    m.fit(df.loc[tr_m, best_feats].values, y[tr_m])
    p_jun = m.predict_proba(df.loc[te_m, best_feats].values)[:, 1]
    thr_g, _ = tune_threshold(y[tr_m], m.predict_proba(
        df.loc[tr_m, best_feats].values)[:, 1], 0.90)
    mg = metrics_at(y[te_m], p_jun, thr_g)
    print(f"\nTrain oct-dic25 -> test jun26 ({y[te_m].sum()} FAIL): "
          f"R={mg['recall']:.3f} P={mg['precision']:.4f} "
          f"(threshold del train={thr_g:.4f})")

# ============================================================== 10. ERRORES
hr("10. ERROR ANALYSIS")
fn_mask = (y == 1) & (ml_pred == 0)
print(f"False Negatives (escapes): {fn_mask.sum()} de {y.sum()} FAIL")
if fn_mask.sum():
    cols_err = ["SerialNumber", "Fecha", "S_High_mVA", "Proy_220A_High_mV",
                "Consumo_mA", "Tiempo_ms_High", "_proba"]
    print(df.loc[fn_mask, cols_err].sort_values("_proba")
          .to_string(index=False, max_rows=30))
    print("\nS_High de los FN:",
          df.loc[fn_mask, "S_High_mVA"].describe()[["min", "mean", "max"]].round(3).to_dict())
fp = df[(df.y == 0)].nlargest(15, "_proba")
print(f"\nTop 15 False Positives extremos (PASS con mayor P_fail):")
print(fp[["SerialNumber", "Fecha", "S_High_mVA", "Proy_220A_High_mV", "_proba"]]
      .to_string(index=False))

# ============================================================== 11. OUTPUTS
hr("11. OUTPUTS")
# model comparison
plot_res = res_df.reset_index().rename(columns={"index": "model"})
fig, ax = plt.subplots(figsize=(13, 6))
w = 0.2; xpos = np.arange(len(plot_res))
for i, (col, lab) in enumerate([("rec90_recall", "Recall@90%"),
                                ("rec90_precision", "Precision@R90"),
                                ("f1_f1", "F1 (best)"), ("pr_auc", "PR-AUC")]):
    ax.bar(xpos + i * w, plot_res[col].astype(float), w, label=lab)
ax.set_xticks(xpos + 1.5 * w)
ax.set_xticklabels(plot_res.model, rotation=30, ha="right")
ax.legend(); ax.set_title("Comparación de modelos (OOF, CV agrupada por mes)")
plt.tight_layout()
plt.savefig(os.path.join(OUT, "model_comparison.png"), dpi=130); plt.close()

# confusion matrix best
cm_best = confusion_matrix(y, ml_pred)
plt.figure(figsize=(5.5, 4.5))
sns.heatmap(cm_best, annot=True, fmt="d", cmap="Blues",
            xticklabels=["PASS", "FAIL"], yticklabels=["PASS", "FAIL"])
plt.title(f"{best_key} @ thr={best_thr:.3f}")
plt.ylabel("Real"); plt.xlabel("Predicho")
plt.tight_layout()
plt.savefig(os.path.join(OUT, "confusion_matrix_best.png"), dpi=130); plt.close()

# ROC / PR curves
fig, axes = plt.subplots(1, 2, figsize=(13, 5))
for key in [best_key, "LogReg_SIN_leak"]:
    if key not in oofs: continue
    p = oofs[key]
    fpr, tpr, _ = roc_curve(y, p)
    pr, rc, _ = precision_recall_curve(y, p)
    axes[0].plot(fpr, tpr, label=f"{key} ({roc_auc_score(y, p):.3f})")
    axes[1].plot(rc, pr, label=f"{key} ({average_precision_score(y, p):.3f})")
# baseline point
axes[1].scatter([bR], [bP], c="k", zorder=5, label=f"Regla 3746 (F1={bF:.2f})")
axes[0].plot([0, 1], [0, 1], "k--", lw=.5)
axes[0].set_title("ROC"); axes[0].set_xlabel("FPR"); axes[0].set_ylabel("TPR")
axes[1].set_title("Precision-Recall"); axes[1].set_xlabel("Recall")
axes[1].set_ylabel("Precision")
for a in axes: a.legend(fontsize=8)
plt.tight_layout()
plt.savefig(os.path.join(OUT, "roc_pr_curves.png"), dpi=130); plt.close()

# predictions.csv
pred_out = df[["SerialNumber", "Fecha", "S_High_mVA",
               "Proy_220A_High_mV", "_proba", "Ford_Real"]].copy()
pred_out.columns = ["Serial", "Fecha", "S_High", "Proy_220A", "P_fail", "Ford_Real"]
pred_out["Disposition"] = np.where(disp == 1, "FAIL", "PASS")
pred_out = pred_out[["Serial", "Fecha", "S_High", "Proy_220A", "P_fail",
                     "Disposition", "Ford_Real"]]
pred_out.to_csv(os.path.join(OUT, "predictions.csv"), index=False)
print(f"predictions.csv exportado ({len(pred_out):,} filas)")
res_df.to_csv(os.path.join(OUT, "model_results.csv"))

# ============================================================== RESUMEN
hr("RESUMEN EJECUTIVO")
print(f"""
1. Dataset: {len(df):,} piezas, {y.sum()} FAIL Ford (1:{int((1-y.mean())/y.mean())}), 14 meses.
2. Mejor modelo: {best_key} — PR-AUC OOF = {res_df.iloc[0].pr_auc:.3f}.
3. Regla Proy<3746:  R={bR:.2f} P={bP:.3f} F1={bF:.2f}.
4. Mejor ML @R>=90%: R={mR:.2f} P={mP:.3f} F1={mF:.2f}.
5. Híbrido por zonas: R={hR:.2f} P={hP:.3f} F1={hF:.2f}.
6. Top 3 drivers SHAP: {', '.join(top3)}.
7. Escapes (FN) del ML: {fn_mask.sum()} de {y.sum()}.
8. Recall estable por mes: {'SI' if rec_mes.min() >= 0.75 else 'NO (min=%.2f)' % rec_mes.min()}.
9. ¿ML supera la regla?: {supera}.
10. Recomendación: {'Desplegar híbrido zona+ML con threshold R>=90% y monitoreo mensual de drift.' if supera == 'SI' else 'Mantener regla de sensibilidad; el ML no justifica la complejidad adicional en producción.'}
""")
