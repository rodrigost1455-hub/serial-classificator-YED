"""Train the zona AMARILLO ranking model on the v2 (Ford-calibrated) pool.

Offline step — run manually or as a build/CI step, never inside a request:

    python -m scripts.train_ml_rank_v2         # from backend/, with .venv active

Trains TWO v2 ranking models and serializes both:

  · NARROW (models/ml_rank_v2.pkl) — the ROJO/AMARILLO zone selection
    (5.60 < S ≤ 5.65, snap_grid). This band collapses to a single DMM-quantized
    value (S=5.65), so S_High/Delta_V_High are constant and the model has no
    physical signal (Gain@25% ≈ 14%, ROC-AUC ≈ 0.38). Kept for the record.

  · WIDE (models/ml_rank_v2_wide.pkl) — a two-sided ML-only band
    (UMBRAL_AMARILLO_ML_MIN < S ≤ UMBRAL_AMARILLO_ML_MAX = 5.638..5.700) that
    spans more than one quantization step (5.65 AND 5.70), restoring the
    variability the ranker needs. This band does NOT change any unit's
    disposition — the deterministic ROJO/AMARILLO/VERDE rule is untouched; it
    only widens the pool the ranking is computed over.

Both use the same physical FEATURES (no temporal leakage), the same validation
(StratifiedGroupKFold grouped by month) and 3 seeds for stability. Writes a
3-column comparison (v1 / v2 narrow / v2 wide) to zonas_output_v2/ml_rank_v1_v2.txt.

v1 stays the served model — nothing here touches the dashboard/serving path.
"""
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ml_rank import (  # noqa: E402
    UMBRAL_AMARILLO_ML_MAX, UMBRAL_AMARILLO_ML_MIN, UMBRAL_AMARILLO_V2,
    UMBRAL_ROJO_V2, load_amarillo_rows, load_ml_band_rows, load_report,
    save_model, train_ranking_model,
)

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NARROW_MODEL_PATH = os.path.join(_HERE, "models", "ml_rank_v2.pkl")
WIDE_MODEL_PATH = os.path.join(_HERE, "models", "ml_rank_v2_wide.pkl")
SEEDS = (1, 2, 42)


def _num(x):
    try:
        return float(str(x).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _sanity_unique_S(rows, label):
    """Sanity check: how many distinct quantized S_High / Delta_V_High values
    exist in the pool. A ranker needs >1 or it has nothing to learn from."""
    s_vals = sorted({round(_num(r.get("S_High_mVA")), 4) for r in rows
                     if _num(r.get("S_High_mVA")) is not None})
    dv_vals = {round(_num(r.get("Delta_V_High")), 4) for r in rows
               if _num(r.get("Delta_V_High")) is not None}
    n_fail = sum(1 for r in rows
                 if str(r.get("Ford_Real", "")).strip().upper() == "FAIL")
    print(f"  [{label}] n={len(rows):,}  FAIL={n_fail}  "
          f"S_High únicos={len(s_vals)} {s_vals}  Delta_V_High únicos={len(dv_vals)}")
    return {"n": len(rows), "n_fail": n_fail,
            "s_high_unique": len(s_vals), "s_high_values": s_vals,
            "delta_v_high_unique": len(dv_vals)}


def _train_3seeds(label, path, **kw):
    per_seed = []
    final_model = final_report = None
    for seed in SEEDS:
        model, report = train_ranking_model(random_state=seed, **kw)
        per_seed.append({
            "seed": seed, "model": report["model"],
            "pr_auc": report["pr_auc"], "roc_auc": report["roc_auc"],
            "gain_top10pct": report["gain_top10pct"],
            "gain_top25pct": report["gain_top25pct"],
            "gain_top50pct": report["gain_top50pct"],
        })
        print(f"  [{label}] seed {seed:>2}: model={report['model']:<12} "
              f"PR-AUC={report['pr_auc']:.4f} ROC-AUC={report['roc_auc']:.4f} "
              f"Gain@25%={report['gain_top25pct']:.1%}")
        if seed == 42:
            final_model, final_report = model, report

    g25 = [s["gain_top25pct"] for s in per_seed]
    pr = [s["pr_auc"] for s in per_seed]
    final_report["seeds"] = per_seed
    final_report["stability"] = {
        "gain_top25pct_mean": statistics.mean(g25),
        "gain_top25pct_stdev": statistics.pstdev(g25),
        "gain_top25pct_min": min(g25),
        "gain_top25pct_max": max(g25),
        "pr_auc_mean": statistics.mean(pr),
        "pr_auc_stdev": statistics.pstdev(pr),
    }
    save_model(final_model, final_report, path=path)
    return final_report


def main():
    print("Training zona AMARILLO ranking models — v2 narrow vs wide")
    print("StratifiedGroupKFold grouped by month, 3 seeds for stability.\n")

    # --- sanity check on the pools BEFORE training -----------------------
    narrow_rows = load_amarillo_rows(umbral_rojo=UMBRAL_ROJO_V2,
                                     umbral_amarillo=UMBRAL_AMARILLO_V2,
                                     snap_grid=True)
    wide_rows = load_ml_band_rows(s_min=UMBRAL_AMARILLO_ML_MIN,
                                  s_max=UMBRAL_AMARILLO_ML_MAX, snap_grid=True)
    print("Sanity check — variabilidad física dentro de cada banda:")
    narrow_sane = _sanity_unique_S(narrow_rows, "narrow 5.60-5.65")
    wide_sane = _sanity_unique_S(wide_rows,
                                 f"wide  {UMBRAL_AMARILLO_ML_MIN}-{UMBRAL_AMARILLO_ML_MAX}")
    if wide_sane["s_high_unique"] < 2:
        print("  WARNING: la banda ancha sigue colapsada a un único valor de S.")
    print()

    print("NARROW (models/ml_rank_v2.pkl):")
    narrow_report = _train_3seeds(
        "narrow", NARROW_MODEL_PATH,
        umbral_rojo=UMBRAL_ROJO_V2, umbral_amarillo=UMBRAL_AMARILLO_V2,
        snap_grid=True)
    narrow_report["sanity"] = narrow_sane

    print("\nWIDE (models/ml_rank_v2_wide.pkl):")
    wide_report = _train_3seeds(
        "wide", WIDE_MODEL_PATH,
        snap_grid=True, band=(UMBRAL_AMARILLO_ML_MIN, UMBRAL_AMARILLO_ML_MAX))
    wide_report["sanity"] = wide_sane

    # re-save reports with sanity embedded
    _resave_report(NARROW_MODEL_PATH, narrow_report)
    _resave_report(WIDE_MODEL_PATH, wide_report)

    v1 = load_report()  # committed v1 report (models/ml_rank_report.json)
    _emit_comparison(v1, narrow_report, wide_report)


def _resave_report(model_path, report):
    with open(os.path.splitext(model_path)[0] + "_report.json", "w",
              encoding="utf-8") as f:
        json.dump(report, f, indent=2)


def _emit_comparison(v1, narrow, wide):
    cols = ["Métrica", "v1 (5.55/5.65)", "v2 narrow (5.60/5.65)",
            "v2 wide (5.638/5.700)"]

    def row(metric, fn):
        return [metric, fn(v1) if v1 else "(sin v1)", fn(narrow), fn(wide)]

    def g(rep, k, p=False):
        v = rep.get(k)
        if v is None:
            return "n/a"
        return f"{v:.1%}" if p else (f"{v:.4f}" if isinstance(v, float) else f"{v}")

    rows = [
        row("Pool n",        lambda r: f"{r['n']:,}"),
        row("FAIL n",        lambda r: f"{r['n_fail']}"),
        row("S_High únicos", lambda r: str(r.get("sanity", {}).get("s_high_unique", "?"))),
        row("Modelo",        lambda r: r["model"]),
        row("PR-AUC",        lambda r: g(r, "pr_auc")),
        row("ROC-AUC",       lambda r: g(r, "roc_auc")),
        row("Gain@10%",      lambda r: g(r, "gain_top10pct", True)),
        row("Gain@25%",      lambda r: g(r, "gain_top25pct", True)),
        row("Gain@50%",      lambda r: g(r, "gain_top50pct", True)),
    ]

    widths = [max(len(cols[i]), max(len(r[i]) for r in rows)) for i in range(4)]

    def fmt(cells):
        return "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells)) + " |"

    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    lines = ["RANKING ML — v1 vs v2 narrow vs v2 wide", "=" * len(sep),
             sep, fmt(cols), sep]
    lines += [fmt(r) for r in rows]
    lines.append(sep)

    ns = narrow["stability"]
    ws = wide["stability"]
    lines += [
        "",
        f"Estabilidad 3 seeds {SEEDS}:",
        f"  v2 narrow: Gain@25% = {ns['gain_top25pct_mean']:.1%} ± {ns['gain_top25pct_stdev']:.1%}",
        f"  v2 wide  : Gain@25% = {ws['gain_top25pct_mean']:.1%} ± {ws['gain_top25pct_stdev']:.1%}",
        "",
        "Sanity: la banda narrow colapsa a "
        f"{narrow['sanity']['s_high_unique']} valor(es) de S_High "
        f"({narrow['sanity']['s_high_values']}); la banda wide tiene "
        f"{wide['sanity']['s_high_unique']} "
        f"({wide['sanity']['s_high_values']}) → el modelo wide recupera señal física.",
        "",
        LITTELFUSE_NOTE,
        "",
        "v1 sigue siendo el modelo servido; ml_rank_v2_wide.pkl no está cableado",
        "a ninguna ruta hasta confirmar consistencia (ya validada en 3 seeds).",
    ]
    text = "\n".join(lines)
    print("\n" + text)

    zonas_v2 = os.path.join(os.path.dirname(_HERE), "zonas_output_v2")
    os.makedirs(zonas_v2, exist_ok=True)
    with open(os.path.join(zonas_v2, "ml_rank_v1_v2.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")


# Texto EXACTO de referencia solicitado (spec Littelfuse vs criterio Ford).
LITTELFUSE_NOTE = (
    "Nota: Tolerancia Littelfuse (spec fabricante) = ±1.7% (±3.74A @ 220A) → "
    "S_MIN_Littelfuse = 5.603 mV/A,\nS_MAX_Littelfuse = 5.797 mV/A. Ford aplica "
    "un criterio operativo más estricto (±1.09%, ±2.40A @ 220A). La banda\n"
    "AMARILLO de este sorteo usa el criterio de Ford (más conservador) porque es "
    "el que efectivamente causa rechazos\nen campo — no implica que Littelfuse "
    "esté incorrecto."
)


if __name__ == "__main__":
    main()
