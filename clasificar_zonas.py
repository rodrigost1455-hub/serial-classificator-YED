# -*- coding: utf-8 -*-
"""
Clasificación por ZONAS de sensibilidad con corte temporal — Sorteo Ford / BEC.

Regla de negocio (NO es un modelo ML, es una disposición determinística):

  Corte Littelfuse = Marzo 2026 (MesKey 202603)
    · Fecha  < marzo 2026  -> producción EN RIESGO (stock contaminado)
    · Fecha >= marzo 2026  -> producción LIMPIA (Littelfuse ya corrigió)

  Zonas de S_High_mVA (solo se aplican a la producción EN RIESGO):
    🔴 ROJO      S_High <= 5.55            -> RETIRAR de Ford (falla segura)
    🟡 AMARILLO  5.55 < S_High <= 5.65     -> RIESGO: incluir en sorteo
    🟢 VERDE     S_High > 5.65             -> LIBERAR (probablemente OK)
    🔵 LIMPIO    Fecha >= marzo 2026       -> producción limpia post-corrección

Valida la regla contra Ford_Real (221 FAIL confirmados por Ford Sudáfrica):
cuántas fallas reales cae en cada zona y — lo crítico — cuántas se ESCAPAN
(FAIL real que la regla habría LIBERADO).
"""
import os
import sys
import csv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.abspath(__file__))
CSV_IN = os.path.join(BASE, "CONSOLIDADO_CON_FORD.csv")
OUT_DIR = os.path.join(BASE, "zonas_output")
os.makedirs(OUT_DIR, exist_ok=True)

CORTE = 202603            # marzo 2026: >= es producción limpia
UMBRAL_ROJO = 5.55
UMBRAL_AMARILLO = 5.65

ZONA_ACCION = {
    "ROJO":     "RETIRAR",
    "AMARILLO": "SORTEO",
    "VERDE":    "LIBERAR",
    "LIMPIO":   "LIBERAR",
}


def num(x):
    x = (x or "").strip().replace(",", ".")
    if x in ("", "None"):
        return None
    try:
        return float(x)
    except ValueError:
        return None


def clasificar(meskey, s_high):
    if meskey >= CORTE:
        return "LIMPIO"
    if s_high is None:            # sensor muerto -> se trata como falla segura
        return "ROJO"
    if s_high <= UMBRAL_ROJO:
        return "ROJO"
    if s_high <= UMBRAL_AMARILLO:
        return "AMARILLO"
    return "VERDE"


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


def main():
    all_rows = list(csv.DictReader(open(CSV_IN, encoding="utf-8")))
    n_raw = len(all_rows)
    rows, retest_serials = dedupe_por_serial(all_rows)

    rows_out = []
    # contadores
    zona_total = {z: 0 for z in ZONA_ACCION}
    zona_fail = {z: 0 for z in ZONA_ACCION}
    accion_total = {}
    accion_fail = {}
    total = fail_total = 0

    if True:
        for r in rows:
            total += 1
            meskey = int(r["Anio"]) * 100 + int(r["Mes"])
            s_high = num(r["S_High_mVA"])
            zona = clasificar(meskey, s_high)
            accion = ZONA_ACCION[zona]
            es_fail = r["Ford_Real"].strip().upper() == "FAIL"

            zona_total[zona] += 1
            accion_total[accion] = accion_total.get(accion, 0) + 1
            if es_fail:
                fail_total += 1
                zona_fail[zona] += 1
                accion_fail[accion] = accion_fail.get(accion, 0) + 1

            rows_out.append({
                "SerialNumber": r["SerialNumber"],
                "Fecha": r["Fecha"],
                "MesKey": meskey,
                "S_High_mVA": r["S_High_mVA"],
                "Proy_220A_High_mV": r.get("Proy_220A_High_mV", ""),
                "Margen_Ford_mV": r.get("Margen_Ford_mV", ""),
                "Periodo": "LIMPIO" if meskey >= CORTE else "RIESGO",
                "Zona": zona,
                "Accion": accion,
                "Ford_Real": r["Ford_Real"],
                "Escape": "SI" if (es_fail and accion == "LIBERAR") else "",
            })

    # ---- salida CSV clasificada
    out_csv = os.path.join(OUT_DIR, "clasificacion_zonas.csv")
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        w.writeheader()
        w.writerows(rows_out)

    # ---- lista de escapes (FAIL real que la regla liberaría)
    escapes = [x for x in rows_out if x["Escape"] == "SI"]
    esc_csv = os.path.join(OUT_DIR, "escapes_liberados.csv")
    with open(esc_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        w.writeheader()
        w.writerows(escapes)

    # ============================================================ REPORTE
    def pct(a, b):
        return f"{100*a/b:5.1f}%" if b else "  n/a"

    line = "=" * 72
    print(line)
    print("CLASIFICACIÓN POR ZONAS — Corte Littelfuse = Marzo 2026 (202603)")
    print(line)
    print(f"Registros en el CSV      : {n_raw:,}")
    print(f"Retests consolidados     : {n_raw - total:,} filas de {len(retest_serials)} seriales")
    print(f"Piezas físicas (únicas)  : {total:,}")
    print(f"FAIL reales (piezas)     : {fail_total}  (Ford Sudáfrica)")
    riesgo = sum(v for z, v in zona_total.items() if z != "LIMPIO")
    riesgo_fail = sum(v for z, v in zona_fail.items() if z != "LIMPIO")
    limpio_fail = zona_fail["LIMPIO"]
    print(f"En RIESGO (< mar26): {riesgo:,}  con {riesgo_fail} FAIL")
    print(f"LIMPIO  (>= mar26) : {zona_total['LIMPIO']:,}  con {limpio_fail} FAIL")

    print("\n" + line)
    print(f"{'ZONA':10s}{'ACCIÓN':10s}{'PIEZAS':>10s}{'FAIL':>7s}"
          f"{'%FAIL_zona':>12s}{'%FAILs_capt':>13s}")
    print("-" * 72)
    for z in ["ROJO", "AMARILLO", "VERDE", "LIMPIO"]:
        print(f"{z:10s}{ZONA_ACCION[z]:10s}{zona_total[z]:>10,}{zona_fail[z]:>7}"
              f"{pct(zona_fail[z], zona_total[z]):>12}"
              f"{pct(zona_fail[z], fail_total):>13}")

    print("\n" + line)
    print("EFECTIVIDAD DE LA DISPOSICIÓN (contra Ford_Real)")
    print(line)
    capturados = accion_fail.get("RETIRAR", 0) + accion_fail.get("SORTEO", 0)
    liberados_fail = accion_fail.get("LIBERAR", 0)
    pulled = accion_total.get("RETIRAR", 0) + accion_total.get("SORTEO", 0)
    print(f"Piezas jaladas (RETIRAR+SORTEO) : {pulled:,}")
    print(f"  · FAIL reales dentro          : {capturados}  "
          f"(recall global {pct(capturados, fail_total)})")
    print(f"  · precisión (FAIL/jaladas)    : {pct(capturados, pulled)}")
    print(f"\nESCAPES (FAIL real LIBERADO)     : {len(escapes)}")
    verde_esc = [e for e in escapes if e["Zona"] == "VERDE"]
    limpio_esc = [e for e in escapes if e["Zona"] == "LIMPIO"]
    print(f"  · en VERDE (riesgo, liberado) : {len(verde_esc)}  <- fugas de la regla de zona")
    print(f"  · en LIMPIO (post-corte)      : {len(limpio_esc)}  <- fallas en producción 'limpia'")

    # recall solo sobre producción en riesgo (donde la regla de zona sí aplica)
    print(f"\nRecall de la regla de ZONA (solo producción en riesgo):")
    print(f"  {riesgo_fail - len(verde_esc)}/{riesgo_fail} = "
          f"{pct(riesgo_fail - len(verde_esc), riesgo_fail)} de las fallas en riesgo son jaladas")

    if escapes:
        print("\n" + line)
        print(f"DETALLE DE LOS {len(escapes)} ESCAPES (ordenados por S_High):")
        print("-" * 72)
        print(f"{'Serial':14s}{'Fecha':12s}{'Zona':9s}{'S_High':>8s}{'Proy220':>9s}")
        for e in sorted(escapes, key=lambda x: num(x['S_High_mVA']) or 0):
            sh = num(e['S_High_mVA'])
            pr = num(e['Proy_220A_High_mV'])
            print(f"{e['SerialNumber']:14s}{e['Fecha']:12s}{e['Zona']:9s}"
                  f"{sh:>8.3f}{pr:>9.0f}")

    print("\n" + line)
    print(f"Salidas escritas en: {OUT_DIR}")
    print(f"  · clasificacion_zonas.csv   ({total:,} filas)")
    print(f"  · escapes_liberados.csv     ({len(escapes)} filas)")


if __name__ == "__main__":
    main()
