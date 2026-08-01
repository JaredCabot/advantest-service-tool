"""The reference standards a run is traceable to, and the one place they are asked for.

Two subjects live here, and they are the same subject seen from two ends.

The first is **how the current ranges are measured**. The Operation Manual
offers two methods and recommends neither: the meter's own current terminals in
series with the generator, or the drop across a standard resistor divided by
that resistance. Which is better is not a matter of taste and not a constant --
on a 3458A the DC current function is the weaker one, so at 100 mA the drop
method roughly halves the reference uncertainty, while at 1 mA it depends
entirely on whether the resistor is right for the range. That is why the manual
specifies three of them and why this module asks rather than assumes.

The second is the **certificate's reference-standard table**. Everything the
measurements were traceable to belongs in it: every meter used, and every
standard resistor, with the serial number, calibration due date and
traceability reference of each.

They are one subject because a standard resistor is a reference standard. The
old arrangement had the resistances asked for in one place, the meters' details
asked for in another, and the resistors missing from the certificate entirely.

WHEN THE QUESTIONS ARE ASKED
----------------------------
All of it before the first hookup, not during the run. Two reasons, and the
second is the one that matters:

* the operator has the instruments in front of them and their calibration
  certificates to hand at the start; four hookups in, they have neither
* a question that arrives between two measurements is a question asked while an
  output is settling, and the answer is typed by somebody who wants to get back
  to the run

Where the bus scan already knows an answer, it is offered as the default. SCPI
defines the ``*IDN?`` response as four comma-separated fields -- maker, model,
serial, firmware -- so an instrument that answers that way has already told us
its serial number and the operator only has to press Enter. Pre-SCPI
instruments have not: the 3458A replies to ``ID?`` with the single word
``HP3458A``, and its serial number is on the rear panel where only a human can
read it.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import active
from i18n import t

# --------------------------------------------------------------------------- #
# The instrument's standard resistors
#
# Which current ranges exist, and what the manual specifies for measuring each
# as a voltage drop, are properties of the INSTRUMENT and live in its plugin.
# They are resolved here on each access rather than bound at import, because at
# import time no instrument has been chosen yet.
#
# An instrument with no current ranges -- or one whose manual offers no
# voltage-drop method -- simply defines neither, and every path here degrades to
# "no standards", which is already the direct-measurement case.
# --------------------------------------------------------------------------- #
def _from_instrument(name, default):
    """Read one datum from the bound instrument, or return ``default``.

    Explicitly checks that something is bound rather than relying on the
    exception an unbound read would raise: this is called while assembling a
    certificate, and "no instrument" and "an instrument with no current ranges"
    must reach the same, quiet answer.
    """
    if not active.is_bound():
        return default
    return getattr(active.driver, name, default)


def current_ranges() -> Tuple[str, ...]:
    """The bound instrument's current ranges, in the manual's order."""
    return tuple(_from_instrument("CURRENT_RANGES", ()) or ())


def manual_shunts() -> Dict[str, float]:
    """The standard resistor the manual specifies for each current range."""
    return dict(_from_instrument("MANUAL_SHUNTS", {}) or {})


def __getattr__(name):                       # PEP 562
    """Resolve the two instrument constants at the moment they are read."""
    if name == "CURRENT_RANGES":
        return current_ranges()
    if name == "MANUAL_SHUNTS":
        return manual_shunts()
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


# --------------------------------------------------------------------------- #
# Certificate rows
# --------------------------------------------------------------------------- #
def entry(description: str, model: str = "-", serial: str = "-",
          cal_due: str = "-", traceability: str = "-") -> dict:
    """Build one reference-standard row.

    A fresh dict every time, so two rows describing the same model of
    instrument cannot end up sharing one operator's serial number.
    """
    return dict(description=description, model=model or "-", serial=serial or "-",
                cal_due=cal_due or "-", traceability=traceability or "-")


def dedup(rows: Sequence[dict]) -> List[dict]:
    """Collapse repeated rows to one per (description, model), first seen wins."""
    seen, out = set(), []
    for row in rows:
        key = (row.get("description"), row.get("model"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def serial_from_identity(identity: Optional[str]) -> str:
    """Return the serial number an identity string carries, or ``""``.

    SCPI defines the ``*IDN?`` response as exactly four comma-separated fields,
    the third of which is the serial number, so there is nothing to guess: an
    instrument that answers in that shape has already told us. Anything with
    fewer fields has not, and a few instruments write ``0`` into the field when
    they have nothing to report, which is not a serial number either.
    """
    if not identity:
        return ""
    fields = [field.strip() for field in str(identity).split(",")]
    if len(fields) < 4:
        return ""
    serial = fields[2]
    if serial.lower() in ("", "0", "-", "none", "n/a"):
        return ""
    return serial


def shunt_rows(shunts: Optional[dict] = None,
               limit_ohm: Optional[float] = None) -> List[dict]:
    """Certificate rows for the standard resistors a run actually used.

    ``shunts`` is the per-range mapping when the voltage-drop method was chosen,
    which may cover only some of the ranges -- a range measured directly used no
    standard and so contributes no row. ``limit_ohm`` is the resistor the
    adjustment's current-limit step is measured across, or ``None`` when that
    step was measured directly. When both are given the limit resistor is the
    100 mA standard and shares its row rather than claiming a second one.
    """
    rows: List[dict] = []
    if shunts:
        for name in current_ranges():
            if name not in shunts:
                continue
            rows.append(entry(
                "Standard resistor, %s range (%.9g ohm nominal %.6g)"
                % (name, shunts[name], manual_shunts()[name])))
        if limit_ohm and "100mA" not in shunts:
            rows.append(entry("Standard resistor, current limit (%.9g ohm)"
                              % limit_ohm))
        return rows
    if limit_ohm:
        rows.append(entry("Standard resistor, current limit (%.9g ohm)"
                          % limit_ohm))
    return rows


def collect(console, rows: List[dict]) -> List[dict]:
    """Ask the operator for each standard's certificate details, in place.

    Returns the same list, so a caller can write ``standards = collect(...)``.
    An empty list is returned untouched and asks nothing, which is what happens
    when a run is driven with manual meter entry.
    """
    if not rows:
        return rows
    print("")
    console.hr()
    print(t(" REFERENCE STANDARDS FOR THIS RUN"))
    console.hr()
    print(t("These are what the certificate will claim traceability to."))
    print(t("Press Enter to accept the value in brackets."))
    for row in rows:
        print("")
        print("   " + row["description"])
        if row.get("model", "-") != "-":
            print(t("     model %s") % row["model"])
        if row.get("serial", "-") != "-":
            print(t("     serial number reported by the instrument itself"))
        row["serial"] = _ask(console, t("     Serial number"), row.get("serial"))
        row["cal_due"] = _ask(console, t("     Calibration due"),
                              row.get("cal_due"))
        row["traceability"] = _ask(console, t("     Traceability / cert. no."),
                                   row.get("traceability"))
    return rows


def ask_with_default(console, label: str, default: Optional[str]) -> str:
    """One prompt with a visible default, never returning an empty string.

    Every certificate field the operator types goes through here, so that a
    blank answer means "keep what is shown" everywhere rather than in some
    places, and an unanswered field lands on the certificate as ``-`` rather
    than as an empty space that looks like an oversight.
    """
    default = (default or "-").strip() or "-"
    answer = console.ask("%s [%s]: " % (label, default), default=default)
    return (answer or "").strip() or default


#: The name this was introduced under, kept because it is used throughout this
#: module.
_ask = ask_with_default


# --------------------------------------------------------------------------- #
# Choosing how the current ranges are measured
# --------------------------------------------------------------------------- #
def measure_4w(engine, nominal: float) -> Optional[float]:
    """Offer to measure one standard resistor 4-wire on the reference meter.

    Returns the measured resistance, or ``None`` if it could not be taken. The
    measurement is made here, before the procedure starts, rather than during
    it: 4-wire ohms needs the meter's sense terminals as well as its inputs,
    which is a different connection from the voltage-drop hookup the resistor is
    about to be used in.
    """
    console = engine.console
    print("")
    print(t("   Connect the %.6g ohm standard to the meter FOUR-WIRE:") % nominal)
    print(t("     resistor CURRENT terminals   ->  DMM INPUT HI / LO"))
    print(t("     resistor POTENTIAL terminals ->  DMM SENSE HI / LO"))
    print(t("   Nothing else should be connected to the resistor."))
    if not console.yesno(t("   Connected and ready?"), default=True):
        return None
    try:
        meter = engine.dmm.select("ohmf")
        if meter is None:
            return None
        value = meter.measure_resistance_4w(nominal)
    except Exception as exc:
        print(t("   the resistance measurement failed (%s)") % exc)
        return None
    if value <= 0:
        print(t("   the meter returned %.9g ohm, which is not usable.") % value)
        return None
    error_ppm = (value - nominal) / nominal * 1e6
    print(t("   measured %.9g ohm  (%+.0f ppm from nominal)") % (value, error_ppm))
    if abs(error_ppm) > 10000:
        print(t("   That is more than 1 %% from nominal. Check that the right"))
        print(t("   resistor is connected before accepting it."))
        if not console.yesno(t("   Use it anyway?"), default=False):
            return None
    return value


def _resistance(engine, nominal: float) -> float:
    """Get one standard resistor's value: measured if offered, else typed."""
    console = engine.console
    value = None
    if console.yesno(t("  Measure it 4-wire on the reference meter now?"),
                     default=False):
        value = measure_4w(engine, nominal)
    if value is None:
        value = console.ask_float(
            t("  Its calibrated resistance in ohm [%.6g]: ") % nominal,
            default=nominal)
    value = float(value)
    if value <= 0:
        print(t("  Not a usable resistance; assuming %.6g ohm.") % nominal)
        value = nominal
    return value


def choose_current_method(engine, include_limit_resistor: bool = True
                          ) -> Tuple[Optional[float], Optional[dict]]:
    """Ask how the current ranges are to be measured, and collect the values.

    Returns ``(limit_ohm, shunts)``.

    ``shunts`` maps range name to calibrated resistance for every range that
    will be measured by the Operation Manual's voltage-drop method, and is
    ``None`` when no range will be. It need not cover all three: option 1 asks
    which resistors the operator actually has, and a range with none falls back
    to direct measurement through the meter's own current terminals. That is the
    honest shape of the choice -- a bench with a 10 ohm and no 1 kilohm should
    use it on 100 mA rather than give up the method entirely.

    ``limit_ohm`` is the resistor the *adjustment's* current-limit step is
    measured across, or ``None`` to measure that step directly. It always
    follows the 100 mA range, because the two share a hookup either way; the
    step is never a question of its own. ``include_limit_resistor`` therefore
    now only decides whether the step is mentioned -- a verification has no
    current-limit step and passes ``False``.
    """
    console = engine.console
    print("")
    console.hr()
    print(t(" HOW SHOULD THE CURRENT RANGES BE MEASURED?"))
    console.hr()
    print(t("  1) Voltage drop across standard resistors, where you have them"))
    print(t("     (Operation Manual 7.2.2(11)/(13)/(15): 1 kohm, 100 ohm, "
            "10 ohm)"))
    print(t("     Better on a 3458A -- its DC current function is the weaker"))
    print(t("     one. You will be asked which of the three you have; any"))
    print(t("     range you have no resistor for is measured directly."))
    print(t("  2) Direct current measurement through the meter's I terminals"))
    print(t("     One hookup, no standards at all, and correct; simply a"))
    print(t("     larger reference uncertainty on the 100 mA range."))
    choice = console.ask(t("  Method [2]: "), default="2").strip()

    if not choice.startswith("1"):
        print(t("  Current ranges measured directly, on the meter's own "
                "current terminals."))
        if include_limit_resistor:
            print(t("  The current-limit step is measured directly too, on the"))
            print(t("  same hookup -- so this adjustment needs no resistor at"))
            print(t("  all."))
        return None, None

    shunts: Dict[str, float] = {}
    for range_name in current_ranges():
        nominal = manual_shunts()[range_name]
        print("")
        print(t("  %s range -- the manual specifies a %.6g ohm standard "
                "(1 V at full scale).") % (range_name, nominal))
        if console.yesno(t("  Do you have a %.6g ohm standard resistor?")
                         % nominal, default=True):
            shunts[range_name] = _resistance(engine, nominal)
        else:
            print(t("    %s will be measured directly through the meter "
                    "instead.") % range_name)

    if not shunts:
        print("")
        print(t("  No standards, so every current range is measured directly."))
        if include_limit_resistor:
            print(t("  The current-limit step is measured directly too."))
        return None, None

    print("")
    print(t("  Standards in use:"))
    for range_name in current_ranges():
        if range_name in shunts:
            print(t("    %-6s %.9g ohm") % (range_name, shunts[range_name]))
    direct = [name for name in current_ranges() if name not in shunts]
    if direct:
        print(t("  Measured directly: %s") % ", ".join(direct))
    if include_limit_resistor:
        if "100mA" in shunts:
            print(t("  The current-limit step shares the %.6g ohm standard.")
                  % shunts["100mA"])
        else:
            print(t("  The current-limit step is measured directly, on the"))
            print(t("  direct-connect hookup."))
    return shunts.get("100mA"), shunts
