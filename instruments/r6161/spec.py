"""Published accuracy specification for the Advantest R6161.

Source: Operation Manual section 8.1, "DC Voltage/Current Output
Specifications", table (2) *Total accuracy*. All values are transcribed from the
October 1991 printing.

Accuracy is quoted as two terms that are **added**::

    tolerance = (setting_error_percent / 100) x |setting|  +  range_error

``setting error`` is proportional to the value being generated; ``range error``
is a fixed adder in SI base units. Only the *total* accuracy tables are used
here, because they are what a calibration certificate must be graded against:
the manual defines total accuracy as including "the external standards,
traceability, calibration error, stability, temperature coefficient, long term
stability, linearity, and noise ripple (DC to 1 Hz)". The separate *relative*
accuracy tables exclude the traceability of the external standards and are not
appropriate for certification.

Conditions the specification assumes (manual 8.1(2)):

  * more than one hour of warm-up,
  * 23 +/-5 degrees C, relative humidity 70 % or less,
  * current ranges assured within an output compliance of +/-10 V,
  * line regulation and load regulation are NOT included.

Only 1-day, 90-day and 180-day tables are published; there is no one-year
figure. :data:`DEFAULT_INTERVAL` selects which table the tool grades against.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

# --- unit multipliers, to SI base ------------------------------------------ #
uV, mV, V = 1e-6, 1e-3, 1.0
nA, uA, mA = 1e-9, 1e-6, 1e-3

#: Calibration intervals for which the manual publishes a total-accuracy table.
INTERVALS: Tuple[str, ...] = ("1day", "90day", "180day")

#: The interval the tool grades against unless the operator chooses otherwise.
#: 90 days is the usual working interval for a reference source of this class
#: and is the middle of the three published tables.
DEFAULT_INTERVAL = "90day"

#: Human-readable interval names, for the certificate.
INTERVAL_NAMES: Dict[str, str] = {
    "1day": "24 hours",
    "90day": "90 days",
    "180day": "180 days",
}

# --------------------------------------------------------------------------- #
# Total accuracy: range -> interval -> (setting error %, range error in SI base)
#
# Transcribed verbatim from Operation Manual 8.1(2). The range error is an
# absolute adder, expressed here in volts for the voltage ranges and amps for
# the current ranges.
# --------------------------------------------------------------------------- #
TOTAL_ACCURACY: Dict[str, Dict[str, Tuple[float, float]]] = {
    "10mV":   {"1day": (0.0055, 0.7 * uV),  "90day": (0.0060, 2.3 * uV),  "180day": (0.0065, 2.3 * uV)},
    "100mV":  {"1day": (0.0040, 0.8 * uV),  "90day": (0.0045, 2.5 * uV),  "180day": (0.0050, 2.65 * uV)},
    "1000mV": {"1day": (0.0030, 6 * uV),    "90day": (0.0035, 8 * uV),    "180day": (0.0040, 8 * uV)},
    "1V":     {"1day": (0.0020, 10 * uV),   "90day": (0.0025, 11 * uV),   "180day": (0.0030, 12 * uV)},
    "10V":    {"1day": (0.0020, 60 * uV),   "90day": (0.0025, 70 * uV),   "180day": (0.0030, 70 * uV)},
    "100V":   {"1day": (0.0020, 600 * uV),  "90day": (0.0025, 700 * uV),  "180day": (0.0030, 700 * uV)},
    "1000V":  {"1day": (0.0025, 6 * mV),    "90day": (0.0030, 7 * mV),    "180day": (0.0035, 7 * mV)},
    "1mA":    {"1day": (0.0055, 9 * nA),    "90day": (0.0060, 9 * nA),    "180day": (0.0065, 9 * nA)},
    "10mA":   {"1day": (0.0040, 90 * nA),   "90day": (0.0045, 90 * nA),   "180day": (0.0050, 90 * nA)},
    "100mA":  {"1day": (0.0040, 900 * nA),  "90day": (0.0045, 900 * nA),  "180day": (0.0050, 900 * nA)},
}

# --------------------------------------------------------------------------- #
# Generation range and resolution (Operation Manual 8.1(1))
#
# The generator reaches 1.2 times full range ("overrange"), so a full-scale
# verification point is well within its capability. ``step`` is the minimum
# programmable step, which sets the floor on what a deviation can mean.
# --------------------------------------------------------------------------- #
RESOLUTION: Dict[str, float] = {
    "10mV":   10 * 1e-9,
    "100mV":  100 * 1e-9,
    "1000mV": 1 * uV,
    "1V":     1 * uV,
    "10V":    10 * uV,
    "100V":   100 * uV,
    "1000V":  1 * mV,
    "1mA":    1 * nA,
    "10mA":   10 * nA,
    "100mA":  100 * nA,
}


def tolerance(range_name: str, setting_si: float,
              interval: str = DEFAULT_INTERVAL) -> float:
    """Return the permitted deviation for one point, in SI base units.

    ``setting_si`` is the commanded value in volts or amps; its sign is
    irrelevant because the specification is symmetric. The result is the
    two-term sum described in the module docstring, and is always positive.
    """
    if interval not in INTERVALS:
        raise ValueError("unknown interval %r; expected one of %s"
                         % (interval, ", ".join(INTERVALS)))
    setting_percent, range_error = TOTAL_ACCURACY[range_name][interval]
    return abs(setting_si) * setting_percent / 100.0 + range_error


def tolerance_ppm_of_fs(range_name: str, setting_si: float, fs_si: float,
                        interval: str = DEFAULT_INTERVAL) -> float:
    """Return the same tolerance expressed in ppm of full scale.

    Useful for presenting a whole verification on one scale, since the ranges
    span 10 mV to 1000 V and an absolute deviation is hard to compare across
    them.
    """
    if not fs_si:
        return 0.0
    return tolerance(range_name, setting_si, interval) / fs_si * 1e6


def specification_note(interval: str = DEFAULT_INTERVAL) -> str:
    """Return the sentence the certificate prints beneath the results table."""
    return ("Pass/Fail is assessed against the manufacturer's published total "
            "accuracy specification for the Advantest R6161 (Operation Manual "
            "section 8.1, table 2), %s interval: "
            "+/-(%% of setting + range error). Total accuracy includes the "
            "traceability of the external standards, calibration error, "
            "stability, temperature coefficient, long term stability, "
            "linearity and noise ripple (DC to 1 Hz); line and load regulation "
            "are excluded." % INTERVAL_NAMES[interval])


def conditions_note() -> str:
    """Return the environmental preconditions the specification assumes."""
    return ("The specification assumes more than one hour of warm-up, an "
            "ambient of\n23 +/-5 degrees C and relative humidity of 70 % or "
            "less.\nCurrent ranges are assured within an output compliance of "
            "+/-10 V.")


def summary_rows(interval: str = DEFAULT_INTERVAL) -> List[dict]:
    """Return the specification as display rows, for the tool's Show-spec menu."""
    rows: List[dict] = []
    for range_name, table in TOTAL_ACCURACY.items():
        setting_percent, range_error = table[interval]
        rows.append(dict(range=range_name,
                         setting_percent=setting_percent,
                         range_error=range_error,
                         resolution=RESOLUTION[range_name]))
    return rows
