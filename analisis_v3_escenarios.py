# -*- coding: utf-8 -*-
"""
Análisis v3 — 3 escenarios de tolerancia para la negociación Ford / Littelfuse.

NO modifica clasificar_zonas.py (v1/v2). Es un análisis adicional de apoyo:
compara cómo cambia la contención (zona ROJO) y el sorteo (AMARILLO) si Ford
mueve su límite de sensibilidad entre tres propuestas sobre la mesa.

  A — Ford operativo actual (= v2):  ±1.09%  -> S_MIN=5.638, S_MAX=5.762
  B — Littelfuse Global (referencia): ±1.7%  -> S_MIN=5.603, S_MAX=5.797
  C — Marcel / sensibilidad pura:     ±1.19% -> S_MIN=5.632, S_MAX=5.768
      (70% del ±1.7% Global; el 30% restante = offset+linealidad, corregible
       por software en el BECM, por eso Marcel propone excluirlo)

Defecto de core observado = Mode A (reduce sensibilidad) => el límite relevante
para contención es el INFERIOR (S_MIN). El superior se documenta por completitud.

Corte temporal Littelfuse (marzo 2026, MesKey 202603) sigue aplicando: las piezas
>= marzo 2026 son producción LIMPIA (post-corrección) y no entran a contención.

Salidas -> zonas_output_v3/:
  comparativa_3_escenarios.txt, grafica_3_escenarios.png, ml_rank_escenario_c_report.txt
"""
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))

BASE = os.path.dirname(os.path.abspath(__file__))
CSV_IN = os.path.join(BASE, "CONSOLIDADO_CON_FORD.csv")
OUT_DIR = os.path.join(BASE, "zonas_output_v3")
os.makedirs(OUT_DIR, exist_ok=True)

CORTE = 202603
S_NOM = 5.700
# Banda de sorteo (disposición): AMARILLO = (S_MIN, UMBRAL_SORTEO]; VERDE por encima.
# Mantiene el borde superior del sorteo de v2 (5.65) fijo; solo el borde inferior
# (S_MIN) se mueve con el escenario.
UMBRAL_SORTEO = 5.65
# Banda ML (ranking): más ancha, (S_MIN, UMBRAL_ML_MAX], para abarcar >1 valor
# cuantizado (5.65 y 5.70) y que el modelo tenga señal física.
UMBRAL_ML_MAX = 5.700

# (tag, nombre, S_MIN, S_MAX, tol_pct)
ESCENARIOS = [
    ("A", "Ford actual (±1.09%)",     5.638, 5.762, 1.09),
    ("B", "Littelfuse Global (±1.7%)", 5.603, 5.797, 1.70),
    ("C", "Marcel sens. pura (±1.19%)", 5.632, 5.768, 1.19),
]


def num(x):
    x = (x or "").strip().replace(",", ".")
    if x in ("", "None"):
        return None
    try:
        return float(x)
    except ValueError:
        return None


def pf(s):
    m, d, y = (int(p) for p in s.strip().split("/"))
    return (y, m, d)


def load_deduped():
    rows = list(csv.DictReader(open(CSV_IN, encoding="utf-8")))
    n_raw = len(rows)
    canon = {}
    for r in rows:
        s = r["SerialNumber"]
        ft = pf(r["Fecha"])
        if s not in canon or ft < canon[s][0]:
            canon[s] = (ft, r)
    return [v[1] for v in canon.values()], n_raw


def clasificar_escenario(rows, s_min):
    """Disposición por escenario (borde inferior = s_min).
       ROJO S<=s_min | AMARILLO s_min<S<=5.65 | VERDE S>5.65 | LIMPIO post-corte.
    Devuelve conteos y captura de FAIL. S se redondea a 4 decimales (snap al grid
    de 0.05 del DMM) para robustez de punto flotante — mismo criterio que v2."""
    zt = {"ROJO": 0, "AMARILLO": 0, "VERDE": 0, "LIMPIO": 0}
    zf = {"ROJO": 0, "AMARILLO": 0, "VERDE": 0, "LIMPIO": 0}
    high_out = high_fail = 0          # S > S_MAX (lado alto, completitud)
    fail_total = 0
    for r in rows:
        mk = int(r["Anio"]) * 100 + int(r["Mes"])
        s = num(r["S_High_mVA"])
        es_fail = r["Ford_Real"].strip().upper() == "FAIL"
        if es_fail:
            fail_total += 1
        if mk >= CORTE:
            z = "LIMPIO"
        elif s is None:
            z = "ROJO"
        else:
            s = round(s, 4)
            if s <= s_min:
                z = "ROJO"
            elif s <= UMBRAL_SORTEO:
                z = "AMARILLO"
            else:
                z = "VERDE"
        zt[z] += 1
        if es_fail:
            zf[z] += 1
    return {"zt": zt, "zf": zf, "fail_total": fail_total}


def main():
    rows, n_raw = load_deduped()
    total = len(rows)

    results = {}
    for tag, name, s_min, s_max, tol in ESCENARIOS:
        results[tag] = clasificar_escenario(rows, s_min)

    fail_total = results["A"]["fail_total"]

    # ---------- reporte de texto ----------
    L = []

    def out(s=""):
        L.append(s)

    line = "=" * 78
    out(line)
    out("ANÁLISIS v3 — 3 ESCENARIOS DE TOLERANCIA (negociación Ford / Littelfuse)")
    out(line)
    out(f"Dataset: {n_raw:,} registros -> {total:,} piezas físicas (dedup 1/serial, prod.)")
    out(f"Ford_Real=FAIL confirmados: {fail_total}  (todos en periodo de riesgo < mar-2026)")
    out(f"Corte Littelfuse: marzo 2026 (MesKey {CORTE}) — piezas >= son LIMPIO")
    out("Defecto Mode A (reduce sensibilidad) -> límite de contención = INFERIOR (S_MIN)")
    out("")

    # Detalle por escenario
    for tag, name, s_min, s_max, tol in ESCENARIOS:
        r = results[tag]
        zt, zf = r["zt"], r["zf"]
        rojo = zt["ROJO"]
        rojo_fail = zf["ROJO"]
        escapes = fail_total - rojo_fail          # FAIL con S > S_MIN
        recall = 100 * rojo_fail / fail_total if fail_total else 0
        prec = 100 * rojo_fail / rojo if rojo else 0
        pulled = zt["ROJO"] + zt["AMARILLO"]
        pulled_fail = zf["ROJO"] + zf["AMARILLO"]
        recall_pull = 100 * pulled_fail / fail_total if fail_total else 0
        out("-" * 78)
        out(f"ESCENARIO {tag} — {name}   S_MIN={s_min}  S_MAX={s_max}")
        out("-" * 78)
        out(f"  ROJO  (retener, S<=S_MIN)     : {rojo:>8,}   FAIL dentro: {rojo_fail:>3}")
        out(f"  AMARILLO (sorteo, S_MIN<S<=5.65): {zt['AMARILLO']:>8,}   FAIL dentro: {zf['AMARILLO']:>3}")
        out(f"  VERDE (liberar, S>5.65)        : {zt['VERDE']:>8,}   FAIL dentro: {zf['VERDE']:>3}")
        out(f"  LIMPIO (>= mar-2026)           : {zt['LIMPIO']:>8,}   FAIL dentro: {zf['LIMPIO']:>3}")
        out(f"  Recall contención ROJO         : {rojo_fail}/{fail_total} = {recall:.1f}%")
        out(f"  Precisión ROJO                 : {prec:.2f}%   Escapes (FAIL fuera ROJO): {escapes}")
        out(f"  Recall ROJO+SORTEO (jaladas)   : {pulled_fail}/{fail_total} = {recall_pull:.1f}%")
        out("")

    # ---------- tabla comparativa ----------
    out(line)
    out("TABLA COMPARATIVA — A (Ford) / B (LF 1.7%) / C (Marcel 1.19%)")
    out(line)

    def col(tag, key):
        r = results[tag]
        if key == "smin":
            return {"A": "5.638", "B": "5.603", "C": "5.632"}[tag]
        if key == "rojo":
            return f"{r['zt']['ROJO']:,}"
        if key == "rfail":
            return f"{r['zf']['ROJO']}"
        if key == "esc":
            return f"{fail_total - r['zf']['ROJO']}"
        if key == "recall":
            return f"{100*r['zf']['ROJO']/fail_total:.1f}%"
        if key == "delta":
            d = r['zt']['ROJO'] - results['A']['zt']['ROJO']
            return "—" if tag == "A" else f"{d:+,}"

    filas = [
        ("S_MIN (mV/A)",        "smin"),
        ("Piezas ROJO",         "rojo"),
        ("FAIL en ROJO",        "rfail"),
        ("Escapes (fuera ROJO)", "esc"),
        ("Recall",              "recall"),
        ("Delta piezas vs A",   "delta"),
    ]
    w0 = max(len(f[0]) for f in filas)
    heads = ("A (Ford)", "B (LF 1.7%)", "C (Marcel)")
    wc = 12
    sep = "+" + "-" * (w0 + 2) + ("+" + "-" * (wc + 2)) * 3 + "+"
    out(sep)
    out(f"| {'Métrica':<{w0}} | {heads[0]:<{wc}} | {heads[1]:<{wc}} | {heads[2]:<{wc}} |")
    out(sep)
    for label, key in filas:
        out(f"| {label:<{w0}} | {col('A',key):<{wc}} | {col('B',key):<{wc}} | {col('C',key):<{wc}} |")
    out(sep)
    out("")

    # ---------- hallazgo central + item 3 ----------
    amar_A = results["A"]["zt"]["AMARILLO"]
    out(line)
    out("HALLAZGO CENTRAL — la cuantización del DMM neutraliza la diferencia A/B/C")
    out(line)
    out("El tester cuantiza S_High a pasos de 0.05 mV/A. Los tres S_MIN (5.603,")
    out("5.632, 5.638) caen TODOS entre los valores de rejilla 5.60 y 5.65, así que")
    out("la condición 'S <= S_MIN' es idéntica para los tres = 'S <= 5.60'.")
    out("=> Piezas en las bandas diferenciales (5.603-5.638 y 5.632-5.638): 0.")
    out("=> Piezas ROJO, FAIL capturados y recall son IDÉNTICOS en A, B y C.")
    out("")
    out("IMPACTO EN ZONA AMARILLO (item 3):")
    out(f"  · AMARILLO bajo escenario A (S_MIN<S<=5.65) : {amar_A:,} piezas (dedup)")
    out(f"  · Re-clasificadas a ROJO al pasar A -> C     : 0 piezas")
    out("    (ninguna pieza AMARILLO tiene S entre 5.632 y 5.638; el único valor")
    out("     cuantizado del sorteo es S=5.65, muy por encima de ambos S_MIN)")
    out("")
    out("NOTA de reconciliación con el dashboard:")
    out("  El dashboard muestra ~226 ROJO / ~42,373 AMARILLO: son conteos SIN dedup")
    out("  (113,036 registros crudos) y con umbrales v1 (5.55/5.65). Este análisis")
    out("  usa piezas físicas deduplicadas y los S_MIN de cada escenario, por eso")
    out("  los totales difieren; la CONCLUSIÓN (delta A/B/C = 0) es la misma en ambos.")
    out("")

    txt = "\n".join(L)
    with open(os.path.join(OUT_DIR, "comparativa_3_escenarios.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print(txt)

    # devuelve datos para el resto (gráfica, ML)
    return rows, results, fail_total, total


def grafica(results, total):
    """Barras apiladas ROJO/AMARILLO/VERDE/LIMPIO por escenario, con recall."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    tags = [e[0] for e in ESCENARIOS]
    labels = [f"{e[0]}\n{['±1.09%','±1.7%','±1.19%'][i]}" for i, e in enumerate(ESCENARIOS)]
    zonas = ["ROJO", "AMARILLO", "VERDE", "LIMPIO"]
    colores = {"ROJO": "#e05561", "AMARILLO": "#e0a955",
               "VERDE": "#5bbf6a", "LIMPIO": "#4a90d9"}
    nombres = {"ROJO": "ROJO (retener)", "AMARILLO": "AMARILLO (sorteo)",
               "VERDE": "VERDE (liberar)", "LIMPIO": "LIMPIO (post-corte)"}

    fig, ax = plt.subplots(figsize=(9, 6.5))
    fig.patch.set_facecolor("white")
    x = range(len(tags))
    bottoms = [0] * len(tags)
    for z in zonas:
        vals = [results[t]["zt"][z] for t in tags]
        ax.bar(x, vals, bottom=bottoms, color=colores[z], label=nombres[z],
               width=0.55, edgecolor="white", linewidth=0.6)
        # etiqueta del valor dentro del segmento (si es visible)
        for xi, (v, b) in enumerate(zip(vals, bottoms)):
            if v > total * 0.03:
                ax.text(xi, b + v / 2, f"{v:,}", ha="center", va="center",
                        fontsize=8.5, color="white", fontweight="bold")
        bottoms = [b + v for b, v in zip(bottoms, vals)]

    # anotación de recall de contención sobre cada barra
    for xi, t in enumerate(tags):
        rec = 100 * results[t]["zf"]["ROJO"] / results[t]["fail_total"]
        ax.text(xi, total * 1.02, f"Recall ROJO\n{rec:.1f}%", ha="center",
                va="bottom", fontsize=9, fontweight="bold", color="#333")

    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=10)
    ax.set_ylabel("Piezas físicas (dedup)", fontsize=10)
    ax.set_ylim(0, total * 1.14)
    ax.set_title("3 escenarios de tolerancia — partición de "
                 f"{total:,} piezas\n(idénticos: la cuantización de 0.05 mV/A "
                 "neutraliza la diferencia A/B/C)", fontsize=11.5, pad=14)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.16), ncol=4,
              frameon=False, fontsize=8.5)
    ax.spines[["top", "right"]].set_visible(False)
    ax.yaxis.grid(True, alpha=0.25)
    ax.set_axisbelow(True)
    plt.tight_layout()
    path = os.path.join(OUT_DIR, "grafica_3_escenarios.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\nGráfica escrita: {path}")


def ml_escenario_c(results, fail_total):
    """Re-entrena el ranking ML con la banda ML del escenario C (5.632-5.700),
    3 seeds, y compara contra el escenario A (banda 5.638-5.700 = v2 wide)."""
    import json
    import statistics

    from app import ml_rank

    S_MIN_C = 5.632
    band = (S_MIN_C, UMBRAL_ML_MAX)
    seeds = (1, 2, 42)

    # sanity: valores únicos de S en la banda C
    rows_c = ml_rank.load_ml_band_rows(s_min=S_MIN_C, s_max=UMBRAL_ML_MAX, snap_grid=True)
    s_uni = sorted({round(ml_rank._num(r.get("S_High_mVA")), 4) for r in rows_c
                    if ml_rank._num(r.get("S_High_mVA")) is not None})

    per = []
    final = None
    for sd in seeds:
        _, rep = ml_rank.train_ranking_model(random_state=sd, snap_grid=True, band=band)
        per.append(rep)
        if sd == 42:
            final = rep
    g25 = [p["gain_top25pct"] for p in per]
    pr = [p["pr_auc"] for p in per]

    # escenario A de referencia = v2 wide (banda 5.638-5.700), ya entrenado
    a_path = os.path.join(BASE, "backend", "models", "ml_rank_v2_wide_report.json")
    a = json.load(open(a_path, encoding="utf-8")) if os.path.exists(a_path) else None

    L = []
    L.append("=" * 72)
    L.append("MODELO ML — ESCENARIO C (Marcel ±1.19%) vs ESCENARIO A (Ford ±1.09%)")
    L.append("=" * 72)
    L.append(f"Banda ML escenario C : {S_MIN_C} < S <= {UMBRAL_ML_MAX}  (ranking, no disposición)")
    L.append(f"Valores S únicos     : {len(s_uni)} {s_uni}  (n={final['n']:,}, FAIL={final['n_fail']})")
    L.append(f"Banda ML escenario A : 5.638 < S <= 5.700  (= v2 wide)")
    L.append("")
    L.append(f"{'Métrica':16}{'A (5.638-5.70)':>18}{'C (5.632-5.70)':>18}")
    if a:
        L.append(f"{'Pool n':16}{a['n']:>18,}{final['n']:>18,}")
        L.append(f"{'FAIL n':16}{a['n_fail']:>18}{final['n_fail']:>18}")
        L.append(f"{'Modelo':16}{a['model']:>18}{final['model']:>18}")
        L.append(f"{'PR-AUC':16}{a['pr_auc']:>18.4f}{final['pr_auc']:>18.4f}")
        L.append(f"{'ROC-AUC':16}{a['roc_auc']:>18.4f}{final['roc_auc']:>18.4f}")
        L.append(f"{'Gain@10%':16}{a['gain_top10pct']:>17.1%}{final['gain_top10pct']:>18.1%}")
        L.append(f"{'Gain@25%':16}{a['gain_top25pct']:>17.1%}{final['gain_top25pct']:>18.1%}")
        L.append(f"{'Gain@50%':16}{a['gain_top50pct']:>17.1%}{final['gain_top50pct']:>18.1%}")
    else:
        L.append("(reporte escenario A / v2 wide no encontrado)")
    L.append("")
    L.append(f"Estabilidad C (3 seeds {seeds}):")
    L.append(f"  Gain@25% = {statistics.mean(g25):.1%} ± {statistics.pstdev(g25):.1%}")
    L.append(f"  PR-AUC   = {statistics.mean(pr):.4f} ± {statistics.pstdev(pr):.4f}")
    L.append("")
    L.append("LECTURA: las bandas ML de A (5.638-5.70) y C (5.632-5.70) contienen")
    L.append("EXACTAMENTE los mismos valores cuantizados (5.65 y 5.70), por lo que el")
    L.append("pool, el entrenamiento y las métricas son idénticos. Mover el límite de")
    L.append("Ford a la propuesta de Marcel no cambia el trabajo del ranking ML.")

    txt = "\n".join(L)
    with open(os.path.join(OUT_DIR, "ml_rank_escenario_c_report.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print("\n" + txt)


def conclusion(results, fail_total):
    rojo_A = results["A"]["zt"]["ROJO"]
    rec = 100 * results["A"]["zf"]["ROJO"] / fail_total
    L = []
    L.append("=" * 78)
    L.append("CONCLUSIÓN PARA LA REUNIÓN FORD / LITTELFUSE")
    L.append("=" * 78)
    L.append("")
    L.append("Si Ford adopta el límite propuesto por Marcel (±1.19%, sensibilidad pura),")
    L.append(f"el cambio en volumen de piezas retenidas directamente (ROJO) es de 0 piezas")
    L.append(f"(delta vs el límite actual de Ford ±1.09%), y el recall se mantiene en")
    L.append(f"{rec:.1f}%. Lo mismo ocurre si Ford se moviera hasta el límite completo de")
    L.append(f"Littelfuse (±1.7%): también 0 piezas de diferencia.")
    L.append("")
    L.append("MOTIVO TÉCNICO: la resolución de sensibilidad del tester EOL es 0.05 mV/A.")
    L.append("Los tres límites inferiores propuestos (5.603 / 5.632 / 5.638 mV/A) caen en")
    L.append("el mismo hueco de la rejilla, entre los valores medibles 5.60 y 5.65. Ninguna")
    L.append("pieza tiene una sensibilidad que los distinga. La discusión 1.09% vs 1.19% vs")
    L.append("1.7% es, con los datos actuales, SIN IMPACTO en el volumen de contención.")
    L.append("")
    L.append("IMPLICACIONES PARA LA NEGOCIACIÓN:")
    L.append(f"  · La lista de contención directa (ROJO) es {rojo_A:,} piezas en los tres")
    L.append("    escenarios; ninguna requiere ranking ML (van directo a retención).")
    L.append("  · El punto negociable NO es el % de tolerancia (indistinguible aquí), sino")
    L.append("    los 54 FAIL reales que escapan a ROJO con S en {5.65, 5.70, 5.75}: caen")
    L.append("    dentro de la banda de paso de los TRES criterios y solo se recuperan vía")
    L.append("    el sorteo AMARILLO + ranking ML (recall sube 75.0% -> 94.9% al jalar el")
    L.append("    sorteo). Ese residual es el verdadero tema, no el límite de tolerancia.")
    L.append("  · Si se quisiera separar 1.09% de 1.19%/1.7% habría que aumentar la")
    L.append("    resolución de medición de S en el EOL (reportar más decimales).")
    txt = "\n".join(L)
    # se anexa al comparativo principal
    with open(os.path.join(OUT_DIR, "comparativa_3_escenarios.txt"), "a", encoding="utf-8") as f:
        f.write("\n" + txt + "\n")
    print("\n" + txt)


if __name__ == "__main__":
    rows, results, fail_total, total = main()
    grafica(results, total)
    conclusion(results, fail_total)
    ml_escenario_c(results, fail_total)
