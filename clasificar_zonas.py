# -*- coding: utf-8 -*-
"""
Clasificación por ZONAS de sensibilidad con corte temporal — Sorteo Ford / BEC.

Regla de negocio (NO es un modelo ML, es una disposición determinística):

  Corte Littelfuse = Marzo 2026 (MesKey 202603)
    · Fecha  < marzo 2026  -> producción EN RIESGO (stock contaminado)
    · Fecha >= marzo 2026  -> producción LIMPIA (Littelfuse ya corrigió)

  Zonas de S_High_mVA (solo se aplican a la producción EN RIESGO):
    🔴 ROJO      S_High <= UMBRAL_ROJO         -> RETIRAR de Ford (falla segura)
    🟡 AMARILLO  ROJO < S_High <= AMARILLO      -> RIESGO: incluir en sorteo
    🟢 VERDE     S_High > UMBRAL_AMARILLO       -> LIBERAR (probablemente OK)
    🔵 LIMPIO    Fecha >= marzo 2026            -> producción limpia post-corrección

Valida la regla contra Ford_Real (216 FAIL confirmados por Ford Sudáfrica):
cuántas fallas reales caen en cada zona y — lo crítico — cuántas se ESCAPAN
(FAIL real que la regla habría LIBERADO).

------------------------------------------------------------------------------
VERSIONADO DE UMBRALES (v1 vs v2)
------------------------------------------------------------------------------
  v1 (histórico) : UMBRAL_ROJO=5.55  UMBRAL_AMARILLO=5.65 — derivado por
                   ingeniería inversa de nuestros propios 462 datos (proyección
                   ESTIMADA V_220A < 3746 mV).
  v2 (2026-07)   : UMBRAL_ROJO=5.60  UMBRAL_AMARILLO=5.65 — calibrado contra el
                   criterio REAL de Ford confirmado en su sistema WMA Dashboard
                   (reject code 12250, "ISC System ESR Charge Step 03
                   Drv-Btry Delta Crnt High"): Delta_Crnt > ±2.40 A @ 220 A.

  Conversión del criterio de Ford a sensibilidad:
    Ford tol = ±2.40 A / 220 A = ±1.0909 %
      S_MIN_FORD = 5.700 · (1 - 0.010909) = 5.638 mV/A
      S_MAX_FORD = 5.700 · (1 + 0.010909) = 5.762 mV/A
    Littelfuse spec de manufactura = ±1.7 % (referencia, NO se usa para zonas)
      S_MIN_LITTELFUSE = 5.603 mV/A ; S_MAX_LITTELFUSE = 5.797 mV/A

Ambas versiones se calculan y se escriben a directorios separados
(zonas_output/ para v1, zonas_output_v2/ para v2) para poder comparar.
"""
import os
import sys
import csv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.abspath(__file__))
CSV_IN = os.path.join(BASE, "CONSOLIDADO_CON_FORD.csv")

CORTE = 202603            # marzo 2026: >= es producción limpia

# --- Umbrales de zona (versionados) ------------------------------------------
# v1: histórico, proyección estimada.
UMBRAL_ROJO = 5.55
UMBRAL_AMARILLO = 5.65

# v2: criterio REAL de Ford (WMA Delta_Crnt ±2.40 A @ 220 A).
UMBRAL_ROJO_V2 = 5.60      # margen de seguridad bajo S_MIN_FORD_V2 (5.638)
UMBRAL_AMARILLO_V2 = 5.65  # buffer sobre la banda de paso de Ford

# --- Criterio físico de Ford / Littelfuse ------------------------------------
S_NOMINAL = 5.700
FORD_CMD_CURRENT_A = 220.0
FORD_DELTA_LIMIT_A = 2.40                      # WMA reject 12250: Delta_Crnt > 2.40 A
FORD_TOLERANCE_PCT = 0.010909                  # ±2.40 A @ 220 A, confirmado WMA
LITTELFUSE_TOLERANCE_PCT = 0.017               # ±1.7 % spec fabricante, referencia
S_MIN_FORD_V2 = S_NOMINAL * (1 - FORD_TOLERANCE_PCT)         # 5.638
S_MAX_FORD_V2 = S_NOMINAL * (1 + FORD_TOLERANCE_PCT)         # 5.762
S_MIN_LITTELFUSE = S_NOMINAL * (1 - LITTELFUSE_TOLERANCE_PCT)  # 5.603
S_MAX_LITTELFUSE = S_NOMINAL * (1 + LITTELFUSE_TOLERANCE_PCT)  # 5.797

ZONA_ACCION = {
    "ROJO":     "RETIRAR",
    "AMARILLO": "SORTEO",
    "VERDE":    "LIBERAR",
    "LIMPIO":   "LIBERAR",
}


class ZoneConfig:
    """Parámetros de una versión de la regla de zona.

    ``snap_grid`` redondea S_High a 4 decimales antes de comparar contra los
    umbrales. El tester cuantiza S a pasos de 0.05 mV/A, pero S se calcula como
    ΔV/0.020 y la división en punto flotante introduce ruido sub-ULP
    (p.ej. 0.112/0.020 = 5.600000000000005). Sin redondeo, un umbral que cae
    justo sobre un punto de la rejilla (como 5.60 o 5.65) queda a merced de ese
    ruido: la pieza nominal de 5.60 quedaría FUERA de ``S <= 5.60``. v2 activa
    el redondeo para que el umbral se comporte de forma determinística; v1 se
    mantiene sin él para reproducir exactamente los números históricos.
    """

    def __init__(self, tag, out_dir, umbral_rojo, umbral_amarillo, snap_grid):
        self.tag = tag
        self.out_dir = os.path.join(BASE, out_dir)
        self.umbral_rojo = umbral_rojo
        self.umbral_amarillo = umbral_amarillo
        self.snap_grid = snap_grid


CFG_V1 = ZoneConfig("v1", "zonas_output", UMBRAL_ROJO, UMBRAL_AMARILLO, snap_grid=False)
CFG_V2 = ZoneConfig("v2", "zonas_output_v2", UMBRAL_ROJO_V2, UMBRAL_AMARILLO_V2, snap_grid=True)


def num(x):
    x = (x or "").strip().replace(",", ".")
    if x in ("", "None"):
        return None
    try:
        return float(x)
    except ValueError:
        return None


def clasificar(meskey, s_high, cfg):
    if meskey >= CORTE:
        return "LIMPIO"
    if s_high is None:            # sensor muerto -> se trata como falla segura
        return "ROJO"
    s = round(s_high, 4) if cfg.snap_grid else s_high
    if s <= cfg.umbral_rojo:
        return "ROJO"
    if s <= cfg.umbral_amarillo:
        return "AMARILLO"
    return "VERDE"


def delta_crnt_proy(s_high):
    """Delta_Crnt proyectado a 220 A, replicando la lógica del WMA de Ford.

    El sensor entrega V = offset + S·I. El sistema de Ford lee esa tensión y
    reconstruye la corriente asumiendo la sensibilidad nominal S_NOMINAL:
        Pack_Current_reportado = (V - offset) / S_NOMINAL = S·I / S_NOMINAL
    El offset se cancela (S ya es una pendiente, ΔV/0.020 A, independiente del
    offset). Con I = 220 A comandados (Supply_Current_Act):
        Delta_Crnt = |220 - Pack_Current_reportado|
                   = 220 · |S_NOMINAL - S| / S_NOMINAL

    LIMITACIÓN: no disponemos del dato de corriente real inyectada por Ford
    pieza por pieza, así que usamos la proyección de sensibilidad como proxy.
    Es matemáticamente equivalente a lo que el WMA calcula (el WMA también
    deriva Pack_Current de la tensión vía la sensibilidad nominal), pero no
    captura deriva térmica ni de medición en campo — de ahí que 54 FAIL reales
    caigan dentro de la banda de paso de Ford (ver reporte de discrepancia).
    """
    if s_high is None:
        return None
    return FORD_CMD_CURRENT_A * abs(S_NOMINAL - s_high) / S_NOMINAL


def ford_real_formula(s_high):
    """Disposición calculada según el criterio REAL de Ford (Delta_Crnt>2.40)."""
    dc = delta_crnt_proy(s_high)
    if dc is None:
        return "FAIL"            # sensor muerto -> falla segura
    return "FAIL" if dc > FORD_DELTA_LIMIT_A else "PASS"


def parse_fecha(s):
    """M/D/YYYY -> (yyyy, mm, dd) para ordenar. Robusto a espacios."""
    m, d, y = (int(p) for p in s.strip().split("/"))
    return (y, m, d)


def dedupe_por_serial(rows):
    """Un registro por parte física = su PRODUCCIÓN (test más temprano).

    Los retests (mismo serial, fecha posterior) no son piezas nuevas: se
    consolidan sobre el registro de producción. El periodo riesgo/limpio se
    decide por la fecha de PRODUCCIÓN, no por la del retest — así una pieza de
    nov-2025 reprobada de nuevo en jun-2026 sigue contando como stock en riesgo.
    """
    canon = {}
    retest_serials = set()
    for r in rows:
        s = r["SerialNumber"]
        r["_fecha_t"] = parse_fecha(r["Fecha"])
        if s not in canon:
            canon[s] = r
        else:
            retest_serials.add(s)
            if r["_fecha_t"] < canon[s]["_fecha_t"]:
                canon[s] = r      # conservar el más temprano (producción)
    return list(canon.values()), retest_serials


def clasificar_dataset(rows, cfg):
    """Clasifica las piezas deduplicadas según ``cfg`` y devuelve stats + filas."""
    rows_out = []
    zona_total = {z: 0 for z in ZONA_ACCION}
    zona_fail = {z: 0 for z in ZONA_ACCION}
    accion_total = {}
    accion_fail = {}
    # Concordancia de la fórmula de Ford con el ground truth Ford_Real.
    formula_tp = formula_fp = formula_fn = formula_tn = 0
    total = fail_total = 0

    for r in rows:
        total += 1
        meskey = int(r["Anio"]) * 100 + int(r["Mes"])
        s_high = num(r["S_High_mVA"])
        zona = clasificar(meskey, s_high, cfg)
        accion = ZONA_ACCION[zona]
        es_fail = r["Ford_Real"].strip().upper() == "FAIL"
        dc = delta_crnt_proy(s_high)
        f_formula = ford_real_formula(s_high)

        zona_total[zona] += 1
        accion_total[accion] = accion_total.get(accion, 0) + 1
        if es_fail:
            fail_total += 1
            zona_fail[zona] += 1
            accion_fail[accion] = accion_fail.get(accion, 0) + 1

        # matriz de confusión de la fórmula (solo tiene sentido en riesgo,
        # pero la evaluamos global; toda falla real está en riesgo).
        pred_fail = (f_formula == "FAIL")
        if pred_fail and es_fail:
            formula_tp += 1
        elif pred_fail and not es_fail:
            formula_fp += 1
        elif not pred_fail and es_fail:
            formula_fn += 1
        else:
            formula_tn += 1

        rows_out.append({
            "SerialNumber": r["SerialNumber"],
            "Fecha": r["Fecha"],
            "MesKey": meskey,
            "S_High_mVA": r["S_High_mVA"],
            "Delta_Crnt_A": "" if dc is None else f"{dc:.4f}",
            "Ford_Real_Formula": f_formula,
            "Proy_220A_High_mV": r.get("Proy_220A_High_mV", ""),
            "Margen_Ford_mV": r.get("Margen_Ford_mV", ""),
            "Periodo": "LIMPIO" if meskey >= CORTE else "RIESGO",
            "Zona": zona,
            "Accion": accion,
            "Ford_Real": r["Ford_Real"],
            "Escape": "SI" if (es_fail and accion == "LIBERAR") else "",
        })

    escapes = [x for x in rows_out if x["Escape"] == "SI"]
    return {
        "cfg": cfg,
        "rows_out": rows_out,
        "escapes": escapes,
        "zona_total": zona_total,
        "zona_fail": zona_fail,
        "accion_total": accion_total,
        "accion_fail": accion_fail,
        "total": total,
        "fail_total": fail_total,
        "formula": (formula_tp, formula_fp, formula_fn, formula_tn),
    }


def escribir_csvs(stats):
    cfg = stats["cfg"]
    os.makedirs(cfg.out_dir, exist_ok=True)
    rows_out = stats["rows_out"]
    escapes = stats["escapes"]

    out_csv = os.path.join(cfg.out_dir, "clasificacion_zonas.csv")
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        w.writeheader()
        w.writerows(rows_out)

    esc_csv = os.path.join(cfg.out_dir, "escapes_liberados.csv")
    with open(esc_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        w.writeheader()
        w.writerows(escapes)


def pct(a, b):
    return f"{100*a/b:5.1f}%" if b else "  n/a"


def emitir_reporte(stats, n_raw, retest_serials, echo=True):
    """Genera el reporte de texto para una versión. Devuelve la cadena y,
    si echo, la imprime."""
    cfg = stats["cfg"]
    zona_total = stats["zona_total"]
    zona_fail = stats["zona_fail"]
    accion_total = stats["accion_total"]
    accion_fail = stats["accion_fail"]
    total = stats["total"]
    fail_total = stats["fail_total"]
    escapes = stats["escapes"]
    tp, fp, fn, tn = stats["formula"]

    buf = []

    def out(s=""):
        buf.append(s)

    line = "=" * 72
    out(line)
    out(f"CLASIFICACIÓN POR ZONAS [{cfg.tag}] — Corte Littelfuse = Marzo 2026 (202603)")
    out(f"Umbrales: ROJO <= {cfg.umbral_rojo}   AMARILLO <= {cfg.umbral_amarillo}"
        f"   (snap_grid={cfg.snap_grid})")
    out(line)
    out(f"Registros en el CSV      : {n_raw:,}")
    out(f"Retests consolidados     : {n_raw - total:,} filas de {len(retest_serials)} seriales")
    out(f"Piezas físicas (únicas)  : {total:,}")
    out(f"FAIL reales (piezas)     : {fail_total}  (Ford Sudáfrica)")
    riesgo = sum(v for z, v in zona_total.items() if z != "LIMPIO")
    riesgo_fail = sum(v for z, v in zona_fail.items() if z != "LIMPIO")
    limpio_fail = zona_fail["LIMPIO"]
    out(f"En RIESGO (< mar26): {riesgo:,}  con {riesgo_fail} FAIL")
    out(f"LIMPIO  (>= mar26) : {zona_total['LIMPIO']:,}  con {limpio_fail} FAIL")

    out("\n" + line)
    out(f"{'ZONA':10s}{'ACCIÓN':10s}{'PIEZAS':>10s}{'FAIL':>7s}"
        f"{'%FAIL_zona':>12s}{'%FAILs_capt':>13s}")
    out("-" * 72)
    for z in ["ROJO", "AMARILLO", "VERDE", "LIMPIO"]:
        out(f"{z:10s}{ZONA_ACCION[z]:10s}{zona_total[z]:>10,}{zona_fail[z]:>7}"
            f"{pct(zona_fail[z], zona_total[z]):>12}"
            f"{pct(zona_fail[z], fail_total):>13}")

    out("\n" + line)
    out("EFECTIVIDAD DE LA DISPOSICIÓN (contra Ford_Real)")
    out(line)
    capturados = accion_fail.get("RETIRAR", 0) + accion_fail.get("SORTEO", 0)
    pulled = accion_total.get("RETIRAR", 0) + accion_total.get("SORTEO", 0)
    out(f"Piezas jaladas (RETIRAR+SORTEO) : {pulled:,}")
    out(f"  · FAIL reales dentro          : {capturados}  "
        f"(recall global {pct(capturados, fail_total)})")
    out(f"  · precisión (FAIL/jaladas)    : {pct(capturados, pulled)}")
    out(f"\nESCAPES (FAIL real LIBERADO)     : {len(escapes)}")
    verde_esc = [e for e in escapes if e["Zona"] == "VERDE"]
    limpio_esc = [e for e in escapes if e["Zona"] == "LIMPIO"]
    out(f"  · en VERDE (riesgo, liberado) : {len(verde_esc)}  <- fugas de la regla de zona")
    out(f"  · en LIMPIO (post-corte)      : {len(limpio_esc)}  <- fallas en producción 'limpia'")

    out(f"\nRecall de la regla de ZONA (solo producción en riesgo):")
    out(f"  {riesgo_fail - len(verde_esc)}/{riesgo_fail} = "
        f"{pct(riesgo_fail - len(verde_esc), riesgo_fail)} de las fallas en riesgo son jaladas")

    # --- concordancia de la fórmula de Ford (Delta_Crnt > 2.40 A) ---
    out("\n" + line)
    out("FÓRMULA DE FORD (Ford_Real_Formula: Delta_Crnt proyectado > 2.40 A)")
    out(line)
    out(f"Banda de paso de Ford: S ∈ [{S_MIN_FORD_V2:.3f}, {S_MAX_FORD_V2:.3f}] mV/A")
    out(f"  TP={tp}  FP={fp}  FN={fn}  TN={tn}")
    out(f"  recall vs Ford_Real   : {pct(tp, tp + fn)}  ({tp}/{tp + fn} FAIL reales predichos)")
    out(f"  precisión             : {pct(tp, tp + fp)}")
    out(f"  FN = FAIL reales DENTRO de la banda de paso de Ford: {fn}")
    out(f"       (fallan en campo pero pasan el criterio ±2.40 A — ver discrepancia)")

    if escapes:
        out("\n" + line)
        out(f"DETALLE DE LOS {len(escapes)} ESCAPES (ordenados por S_High):")
        out("-" * 72)
        out(f"{'Serial':14s}{'Fecha':12s}{'Zona':9s}{'S_High':>8s}{'Proy220':>9s}")
        for e in sorted(escapes, key=lambda x: num(x['S_High_mVA']) or 0):
            sh = num(e['S_High_mVA'])
            pr = num(e['Proy_220A_High_mV'])
            out(f"{e['SerialNumber']:14s}{e['Fecha']:12s}{e['Zona']:9s}"
                f"{sh:>8.3f}{pr:>9.0f}")

    out("\n" + line)
    out(f"Salidas escritas en: {cfg.out_dir}")
    out(f"  · clasificacion_zonas.csv   ({total:,} filas)")
    out(f"  · escapes_liberados.csv     ({len(escapes)} filas)")

    text = "\n".join(buf)
    if echo:
        print(text)
    return text


def _load_ml_report(name):
    """Lee un reporte de entrenamiento ML (backend/models/*.json) si existe."""
    import json
    p = os.path.join(BASE, "backend", "models", name)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def tabla_comparativa(stats_v1, stats_v2):
    """Tabla v1 vs v2 pedida en el entregable."""
    def capt(s):
        return s["accion_fail"].get("RETIRAR", 0) + s["accion_fail"].get("SORTEO", 0)

    def recall(s):
        return 100 * capt(s) / s["fail_total"] if s["fail_total"] else 0

    v1, v2 = stats_v1, stats_v2
    rows = [
        ("Umbral ROJO",        f"{v1['cfg'].umbral_rojo}",           f"{v2['cfg'].umbral_rojo}"),
        ("Umbral AMARILLO",    f"{v1['cfg'].umbral_amarillo}",       f"{v2['cfg'].umbral_amarillo}"),
        ("snap_grid (fix FP)", f"{v1['cfg'].snap_grid}",             f"{v2['cfg'].snap_grid}"),
        ("Piezas ROJO",        f"{v1['zona_total']['ROJO']:,}",      f"{v2['zona_total']['ROJO']:,}"),
        ("Piezas AMARILLO",    f"{v1['zona_total']['AMARILLO']:,}",  f"{v2['zona_total']['AMARILLO']:,}"),
        ("Piezas VERDE",       f"{v1['zona_total']['VERDE']:,}",     f"{v2['zona_total']['VERDE']:,}"),
        ("FAIL en ROJO",       f"{v1['zona_fail']['ROJO']}",         f"{v2['zona_fail']['ROJO']}"),
        ("FAIL en AMARILLO",   f"{v1['zona_fail']['AMARILLO']}",     f"{v2['zona_fail']['AMARILLO']}"),
        ("Escapes (VERDE)",    f"{v1['zona_fail']['VERDE']}",        f"{v2['zona_fail']['VERDE']}"),
        ("Recall jaladas",     f"{recall(v1):.1f}%",                 f"{recall(v2):.1f}%"),
    ]

    # Fila Gain@25% ML, leída de los reportes si los modelos ya se entrenaron.
    ml_v1 = _load_ml_report("ml_rank_report.json")
    ml_v2 = _load_ml_report("ml_rank_v2_report.json")

    def g25(rep):
        return f"{100*rep['gain_top25pct']:.1f}%" if rep else "(sin entrenar)"

    rows.append(("Gain@25% ML", g25(ml_v1), g25(ml_v2)))
    if ml_v1 and ml_v2:
        rows.append(("PR-AUC ML", f"{ml_v1['pr_auc']:.4f}", f"{ml_v2['pr_auc']:.4f}"))

    w0 = max(len(r[0]) for r in rows)
    line = "+" + "-" * (w0 + 2) + "+" + "-" * 16 + "+" + "-" * 16 + "+"
    buf = [line,
           f"| {'Métrica':<{w0}} | {'v1 (viejo)':<14} | {'v2 (nuevo)':<14} |",
           line]
    for name, a, b in rows:
        buf.append(f"| {name:<{w0}} | {a:<14} | {b:<14} |")
    buf.append(line)
    buf.append("")
    buf.append("LECTURA DEL RESULTADO")
    buf.append("  · La regla DETERMINÍSTICA de zona MEJORA con v2: los escapes")
    buf.append("    (FAIL liberado en VERDE) bajan de 23 a 11 y el recall de piezas")
    buf.append("    jaladas sube de 89.4% a 94.9%. Las 147 piezas FAIL con S=5.60")
    buf.append("    (banda de rechazo real de Ford, S<5.638) pasan de SORTEO a")
    buf.append("    RETIRAR — ya no dependen de un sorteo.")
    buf.append("  · El ranking ML EMPEORA con v2 (Gain@25% 84%->14%, ROC-AUC ~0.38).")
    buf.append("    Motivo: la zona AMARILLO v2 colapsa a un único valor cuantizado")
    buf.append("    S=5.65 (el tester cuantiza S a pasos de 0.05), así que S_High y")
    buf.append("    Delta_V_High —los features dominantes— son CONSTANTES en el pool")
    buf.append("    y el modelo se queda sin señal física discriminante.")
    buf.append("  · Conclusión: v2 es mejor globalmente porque mueve la señal fuerte")
    buf.append("    al criterio determinístico (ROJO); el ML deja de aportar en el")
    buf.append("    residual duro. Si se quiere triaje ML del sorteo v2, la banda")
    buf.append("    AMARILLO debe abarcar >1 paso de cuantización (p.ej. 5.638–5.70).")
    return "\n".join(buf)


DISCREPANCIA_DOC = f"""\
================================================================================
HALLAZGO TÉCNICO — Discrepancia de tolerancia FORD vs LITTELFUSE
================================================================================

EVIDENCIA
  Sistema WMA Dashboard de Ford, reject code 12250:
    "ISC System ESR Charge Step 03 Drv-Btry Delta Crnt High"
  Criterio de rechazo confirmado en campo:
    Delta_Crnt = |Supply_Current_Act - Pack_Current_reportado| > 2.40 A
    sobre una corriente comandada de 220 A.

CONVERSIÓN A SENSIBILIDAD (S, mV/A; nominal {S_NOMINAL})
  Ford (criterio de sistema completo, confirmado WMA):
    tolerancia = ±{FORD_DELTA_LIMIT_A} A / {FORD_CMD_CURRENT_A:.0f} A = ±{100*FORD_TOLERANCE_PCT:.4f} %
      S_MIN_FORD = {S_NOMINAL} · (1 - {FORD_TOLERANCE_PCT}) = {S_MIN_FORD_V2:.3f} mV/A
      S_MAX_FORD = {S_NOMINAL} · (1 + {FORD_TOLERANCE_PCT}) = {S_MAX_FORD_V2:.3f} mV/A

  Littelfuse (spec de manufactura del sensor, REFERENCIA — no se usa para zonas):
    tolerancia = ±{100*LITTELFUSE_TOLERANCE_PCT:.1f} %  (±{S_NOMINAL*LITTELFUSE_TOLERANCE_PCT*FORD_CMD_CURRENT_A/S_NOMINAL:.2f} A @ 220 A)
      S_MIN_LITTELFUSE = {S_MIN_LITTELFUSE:.3f} mV/A
      S_MAX_LITTELFUSE = {S_MAX_LITTELFUSE:.3f} mV/A

QUÉ SIGNIFICA LA DISCREPANCIA
  El criterio de Ford (±1.09 %) es MÁS ESTRECHO que la spec de manufactura de
  Littelfuse (±1.7 %). No implica que Littelfuse esté equivocado: son bases de
  comparación distintas —
    · Littelfuse valida la MANUFACTURA del componente (¿el sensor cumple su
      hoja de datos?).
    · Ford valida el SISTEMA COMPLETO en operación (¿la lectura de corriente
      del ISC concuerda con la corriente real de la batería?).
  Usamos el criterio de FORD como ground truth para el sorteo porque es el que
  EFECTIVAMENTE rechaza piezas en campo, no porque Littelfuse esté equivocado.

BANDA GRIS DE NEGOCIACIÓN (escalación)
  Piezas con S entre {S_MIN_LITTELFUSE:.3f} y {S_MIN_FORD_V2:.3f} mV/A están DENTRO de la
  tolerancia de manufactura de Littelfuse pero FUERA del criterio operativo de
  Ford. Son candidatas a NEGOCIACIÓN con Ford si el volumen es significativo:
  técnicamente cumplen la spec del proveedor pero disparan el reject 12250.

LIMITACIÓN DE LA FÓRMULA (Ford_Real_Formula)
  Delta_Crnt se proyecta desde la sensibilidad medida a 20 A:
    Delta_Crnt = 220 · |S_NOMINAL - S| / S_NOMINAL
  No disponemos de la corriente real inyectada por Ford pieza por pieza, así que
  la proyección de sensibilidad es un PROXY (matemáticamente equivalente a lo
  que el WMA calcula, que también deriva Pack_Current de la tensión vía la
  sensibilidad nominal). El proxy NO captura deriva térmica ni de medición en
  campo: por eso hay FAIL reales que caen DENTRO de la banda de paso de Ford
  (ver "FN" en el reporte de zonas). El criterio de Ford explica la mayoría de
  los rechazos, pero no todos — el sorteo de la zona AMARILLO + el ranking ML
  cubren el residual.
"""


def main():
    all_rows = list(csv.DictReader(open(CSV_IN, encoding="utf-8")))
    n_raw = len(all_rows)
    rows, retest_serials = dedupe_por_serial(all_rows)

    # v1 (histórico) y v2 (criterio real de Ford) sobre el MISMO set deduplicado.
    stats_v1 = clasificar_dataset(rows, CFG_V1)
    stats_v2 = clasificar_dataset(rows, CFG_V2)

    escribir_csvs(stats_v1)
    escribir_csvs(stats_v2)

    # Reporte v1 a consola + archivo (mantiene compatibilidad histórica).
    txt_v1 = emitir_reporte(stats_v1, n_raw, retest_serials, echo=True)
    with open(os.path.join(CFG_V1.out_dir, "reporte_zonas.txt"), "w", encoding="utf-8") as f:
        f.write(txt_v1 + "\n")

    print("\n\n" + "#" * 72)
    print("#  VERSIÓN v2 — CRITERIO REAL DE FORD (WMA Delta_Crnt ±2.40 A)")
    print("#" * 72 + "\n")

    txt_v2 = emitir_reporte(stats_v2, n_raw, retest_serials, echo=True)
    with open(os.path.join(CFG_V2.out_dir, "reporte_zonas.txt"), "w", encoding="utf-8") as f:
        f.write(txt_v2 + "\n")

    # Tabla comparativa v1 vs v2.
    tabla = tabla_comparativa(stats_v1, stats_v2)
    print("\n\n" + "=" * 72)
    print("COMPARATIVA v1 vs v2")
    print("=" * 72)
    print(tabla)
    with open(os.path.join(CFG_V2.out_dir, "comparativa_v1_v2.txt"), "w", encoding="utf-8") as f:
        f.write("COMPARATIVA v1 vs v2\n" + "=" * 72 + "\n" + tabla + "\n")

    # Documento de discrepancia Ford/Littelfuse.
    with open(os.path.join(CFG_V2.out_dir, "discrepancia_ford_littelfuse.txt"),
              "w", encoding="utf-8") as f:
        f.write(DISCREPANCIA_DOC)
    print("\n" + DISCREPANCIA_DOC)


if __name__ == "__main__":
    main()
