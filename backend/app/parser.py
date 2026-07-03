"""Raw EOL log (`.txt`) parser — defensive by design.

The exact byte layout of the tester logs is not fixed (§1 warns about whitespace
and locale: commas-as-decimal appear in some files). So rather than assume column
positions, we locate each value by the *section label* the spec names and read the
first number that follows it. Label patterns live in ``LABELS`` and can be tuned
without touching the extraction logic.

If any of the four voltages needed for the physics is missing, the file is routed
to quarantine and a parse_error Unit is emitted (the caller does the file move).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import List, Optional

from .calc import RawFields
from .config import VALID_COHORTS, DEFAULT_COHORT

# 11-digit run inside the filename, e.g. "7370-2573-8W 26168000568 0101 OK.txt"
SERIAL_RE = re.compile(r"\b(\d{11})\b")

# A signed decimal that tolerates either '.' or ',' as the separator.
NUMBER_RE = re.compile(r"[-+]?\d{1,3}(?:[.,]\d+)?|\d+")

# Section labels -> attribute. Case-insensitive, matched anywhere on a line.
LABELS = {
    "Consumo_mA": r"CONSUMPTION\s+CURRENT",
    "Offset_High_V": r"HIGH\s+RANGE\s+OUTPUT\s+VOLTAGE",
    "Offset_Low_V": r"LOW\s+RANGE\s+OUTPUT\s+VOLTAGE",
    "V_20A_High_V": r"VOLTAJE\s+C3-?C2",   # inside the POLARITY block
    "V_20A_Low_V": r"VOLTAJE\s+C3-?C4",
}

REQUIRED = ("Offset_High_V", "V_20A_High_V", "Offset_Low_V", "V_20A_Low_V")


def parse_number(text: str) -> Optional[float]:
    """First number on a line, tolerant of ',' or '.' decimals. None if absent."""
    m = NUMBER_RE.search(text)
    if not m:
        return None
    return float(m.group(0).replace(",", "."))


def _first_number_after_label(lines: List[str], pattern: str) -> Optional[float]:
    rx = re.compile(pattern, re.IGNORECASE)
    for i, line in enumerate(lines):
        if rx.search(line):
            # value may sit on the same line after the label, or on the next line
            after = rx.split(line, maxsplit=1)[-1]
            n = parse_number(after)
            if n is not None:
                return n
            if i + 1 < len(lines):
                return parse_number(lines[i + 1])
    return None


def _tiempos(lines: List[str]) -> List[float]:
    """4th field of each MedirVoltajeDMM line, in document order.

    Assumption: the first MedirVoltajeDMM reading is the High-range stabilization
    time, the second is the Low-range one. Fields split on comma/whitespace/tab.
    """
    out: List[float] = []
    rx = re.compile(r"MedirVoltajeDMM", re.IGNORECASE)
    for line in lines:
        if not rx.search(line):
            continue
        # Split the whole line; the label token counts as field 1, so the
        # stabilization time is the 4th field counting from the token.
        fields = [f for f in re.split(r"[,\s;]+", line.strip()) if f != ""]
        tok = next((i for i, f in enumerate(fields)
                    if f.lower().startswith("medirvoltajedmm")), None)
        if tok is None or tok + 3 >= len(fields):
            continue
        try:
            out.append(float(fields[tok + 3].replace(",", ".")))
        except ValueError:
            pass
    return out


def _estatus(lines: List[str], filename: str) -> Optional[str]:
    """Tester verdict (OK/NG). Prefer an explicit verdict line; fall back to the
    filename token (a run marked NG in the name)."""
    for line in lines:
        m = re.search(r"\b(RESULT|RESULTADO|VEREDICTO|STATUS|ESTATUS)\b.*\b(OK|NG)\b",
                      line, re.IGNORECASE)
        if m:
            return m.group(2).upper()
    m = re.search(r"\b(OK|NG)\b", filename, re.IGNORECASE)
    return m.group(1).upper() if m else None


def extract_serial(filename: str) -> Optional[str]:
    m = SERIAL_RE.search(filename)
    return m.group(1) if m else None


def normalize_cohort(cohort: Optional[str]) -> str:
    if cohort and cohort.upper() in VALID_COHORTS:
        return cohort.upper()
    return DEFAULT_COHORT


def file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def parse(content: bytes, filename: str, cohort: Optional[str] = None) -> RawFields:
    """Parse one raw log's bytes into RawFields. Never raises — a parse failure is
    recorded in ``parse_error`` so the pipeline can quarantine and continue."""
    text = content.decode("utf-8", errors="replace")
    lines = text.splitlines()

    serial = extract_serial(filename) or ""
    raw = RawFields(
        Categoria=normalize_cohort(cohort),
        SerialNumber=serial,
        Archivo=filename,
    )

    raw.Estatus_Tester = _estatus(lines, filename)
    raw.Consumo_mA = _first_number_after_label(lines, LABELS["Consumo_mA"])
    raw.Offset_High_V = _first_number_after_label(lines, LABELS["Offset_High_V"])
    raw.Offset_Low_V = _first_number_after_label(lines, LABELS["Offset_Low_V"])
    raw.V_20A_High_V = _first_number_after_label(lines, LABELS["V_20A_High_V"])
    raw.V_20A_Low_V = _first_number_after_label(lines, LABELS["V_20A_Low_V"])

    tiempos = _tiempos(lines)
    raw.Tiempo_ms_High = tiempos[0] if len(tiempos) >= 1 else None
    raw.Tiempo_ms_Low = tiempos[1] if len(tiempos) >= 2 else None

    missing = [f for f in REQUIRED if getattr(raw, f) is None]
    if not serial:
        missing.append("SerialNumber")
    if missing:
        raw.parse_error = "parse_error"
    return raw
