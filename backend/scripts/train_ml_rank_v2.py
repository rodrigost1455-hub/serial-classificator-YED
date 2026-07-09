"""Train the zona AMARILLO ranking model on the v2 (Ford-calibrated) pool.

Offline step — run manually or as a build/CI step, never inside a request:

    python -m scripts.train_ml_rank_v2         # from backend/, with .venv active

v2 differs from scripts/train_ml_rank.py only in which rows land in zona
AMARILLO: it uses UMBRAL_ROJO_V2=5.60 / UMBRAL_AMARILLO_V2=5.65 with grid-snap
rounding (Ford's real WMA criterion, Delta_Crnt > ±2.40 A @ 220 A → S_MIN=5.638)
instead of v1's 5.55/5.65. Features, validation scheme (StratifiedGroupKFold by
month) and metrics are identical, so v1 vs v2 is an apples-to-apples comparison.

Trains across 3 seeds to confirm the ranking is stable (not overfit to fold
composition), then serializes the seed-42 model + a report to
backend/models/ml_rank_v2.pkl / ml_rank_v2_report.json. The report embeds the
per-seed Gain@10/25/50% so the v1-vs-v2 comparison is reproducible.
"""
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ml_rank import (  # noqa: E402
    UMBRAL_AMARILLO_V2, UMBRAL_ROJO_V2, load_report, save_model,
    train_ranking_model,
)

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V2_MODEL_PATH = os.path.join(_HERE, "models", "ml_rank_v2.pkl")
SEEDS = (1, 2, 42)


def main():
    print("Training zona AMARILLO ranking model — v2 (Ford ±2.40 A, "
          f"ROJO<={UMBRAL_ROJO_V2}/AMARILLO<={UMBRAL_AMARILLO_V2}, snap_grid)")
    print("StratifiedGroupKFold grouped by month, 3 seeds for stability.\n")

    per_seed = []
    final_model = final_report = None
    for seed in SEEDS:
        model, report = train_ranking_model(
            random_state=seed,
            umbral_rojo=UMBRAL_ROJO_V2, umbral_amarillo=UMBRAL_AMARILLO_V2,
            snap_grid=True,
        )
        per_seed.append({
            "seed": seed, "model": report["model"],
            "pr_auc": report["pr_auc"], "roc_auc": report["roc_auc"],
            "gain_top10pct": report["gain_top10pct"],
            "gain_top25pct": report["gain_top25pct"],
            "gain_top50pct": report["gain_top50pct"],
        })
        print(f"  seed {seed:>2}: model={report['model']:<12} "
              f"PR-AUC={report['pr_auc']:.4f} ROC-AUC={report['roc_auc']:.4f} "
              f"Gain@25%={report['gain_top25pct']:.1%}")
        if seed == 42:
            final_model, final_report = model, report

    g25 = [s["gain_top25pct"] for s in per_seed]
    pr = [s["pr_auc"] for s in per_seed]
    stability = {
        "gain_top25pct_mean": statistics.mean(g25),
        "gain_top25pct_stdev": statistics.pstdev(g25),
        "gain_top25pct_min": min(g25),
        "gain_top25pct_max": max(g25),
        "pr_auc_mean": statistics.mean(pr),
        "pr_auc_stdev": statistics.pstdev(pr),
    }
    final_report["seeds"] = per_seed
    final_report["stability"] = stability

    path = save_model(final_model, final_report, path=V2_MODEL_PATH)

    # v1 report (if the v1 model has been trained) for a side-by-side print.
    v1 = load_report()

    print(f"\n{'':16}{'v1 (5.55/5.65)':>18}{'v2 (5.60/5.65)':>18}")
    if v1:
        print(f"{'AMARILLO n':16}{v1['n']:>18,}{final_report['n']:>18,}")
        print(f"{'FAIL n':16}{v1['n_fail']:>18}{final_report['n_fail']:>18}")
        print(f"{'PR-AUC':16}{v1['pr_auc']:>18.4f}{final_report['pr_auc']:>18.4f}")
        print(f"{'Gain@25%':16}{v1['gain_top25pct']:>17.1%}{final_report['gain_top25pct']:>18.1%}")
    else:
        print(f"{'AMARILLO n':16}{'(v1 not trained)':>18}{final_report['n']:>18,}")

    print(f"\nStability across seeds {SEEDS}:")
    print(f"  Gain@25% = {stability['gain_top25pct_mean']:.1%} "
          f"± {stability['gain_top25pct_stdev']:.1%} "
          f"[{stability['gain_top25pct_min']:.1%}, {stability['gain_top25pct_max']:.1%}]")
    print(f"  PR-AUC   = {stability['pr_auc_mean']:.4f} ± {stability['pr_auc_stdev']:.4f}")

    print(f"\nSaved model to  : {path}")
    print(f"Saved report to : {os.path.splitext(path)[0]}_report.json")

    # Also drop a plaintext v1-vs-v2 comparison into zonas_output_v2/ so the
    # deliverable's Gain@25% row lives next to the zone comparison.
    zonas_v2 = os.path.join(os.path.dirname(_HERE), "zonas_output_v2")
    os.makedirs(zonas_v2, exist_ok=True)
    with open(os.path.join(zonas_v2, "ml_rank_v1_v2.txt"), "w", encoding="utf-8") as f:
        f.write("RANKING ML — zona AMARILLO v1 vs v2\n")
        f.write("=" * 60 + "\n")
        if v1:
            f.write(f"{'Métrica':16}{'v1 (5.55/5.65)':>20}{'v2 (5.60/5.65)':>20}\n")
            f.write(f"{'AMARILLO n':16}{v1['n']:>20,}{final_report['n']:>20,}\n")
            f.write(f"{'FAIL n':16}{v1['n_fail']:>20}{final_report['n_fail']:>20}\n")
            f.write(f"{'model':16}{v1['model']:>20}{final_report['model']:>20}\n")
            f.write(f"{'PR-AUC':16}{v1['pr_auc']:>20.4f}{final_report['pr_auc']:>20.4f}\n")
            f.write(f"{'ROC-AUC':16}{v1['roc_auc']:>20.4f}{final_report['roc_auc']:>20.4f}\n")
            f.write(f"{'Gain@10%':16}{v1['gain_top10pct']:>19.1%}{final_report['gain_top10pct']:>20.1%}\n")
            f.write(f"{'Gain@25%':16}{v1['gain_top25pct']:>19.1%}{final_report['gain_top25pct']:>20.1%}\n")
            f.write(f"{'Gain@50%':16}{v1['gain_top50pct']:>19.1%}{final_report['gain_top50pct']:>20.1%}\n")
        else:
            f.write("(v1 report not found — run scripts/train_ml_rank.py first)\n")
        f.write("\nEstabilidad v2 (3 seeds " + str(SEEDS) + "):\n")
        f.write(f"  Gain@25% = {stability['gain_top25pct_mean']:.1%} "
                f"± {stability['gain_top25pct_stdev']:.1%}\n")
        f.write(f"  PR-AUC   = {stability['pr_auc_mean']:.4f} "
                f"± {stability['pr_auc_stdev']:.4f}\n")


if __name__ == "__main__":
    main()
