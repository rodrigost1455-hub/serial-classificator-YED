"""Train the zona AMARILLO ranking model and serialize it.

Offline step — run manually or as a build/CI step, never inside a request:

    python -m scripts.train_ml_rank            # from backend/, with .venv active

Writes backend/models/ml_rank.pkl (model + feature list) and
backend/models/ml_rank_report.json (OOF metrics: PR-AUC, ROC-AUC, gain@10/25/50%).
The dashboard KPI and GET /api/ml-rank/meta both read the report — retrain
whenever CONSOLIDADO_CON_FORD.csv is refreshed so the quoted numbers stay current.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ml_rank import MODEL_PATH, save_model, train_ranking_model  # noqa: E402


def main():
    print("Training zona AMARILLO ranking model (StratifiedGroupKFold, grouped by month)...")
    model, report = train_ranking_model()
    path = save_model(model, report)

    print(f"\nBest model     : {report['model']}")
    print(f"Trained on      : {report['n']:,} zona AMARILLO units, {report['n_fail']} Ford_Real=FAIL")
    print(f"Features        : {report['features']}")
    print(f"OOF PR-AUC      : {report['pr_auc']:.4f}")
    print(f"OOF ROC-AUC     : {report['roc_auc']:.4f}")
    print(f"Gain top 10%    : {report['gain_top10pct']:.1%} of known FAILs")
    print(f"Gain top 25%    : {report['gain_top25pct']:.1%} of known FAILs")
    print(f"Gain top 50%    : {report['gain_top50pct']:.1%} of known FAILs")
    print(f"\nSaved model to  : {path}")
    print(f"Saved report to : {os.path.splitext(path)[0]}_report.json")


if __name__ == "__main__":
    main()
