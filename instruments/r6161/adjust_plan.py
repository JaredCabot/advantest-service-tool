"""Adjustment point catalogue for the Advantest R6161.

This is the complete manual 7.2.2 routine expressed as GPIB point selectors --
45 points in four hookup groups. Every selector here was recovered from firmware
A05 by disassembly; none of them appears in the Operation Manual.

    3   DAC connection coefficients   RKTA / RKTB / RKTD
    40  range points                  RV<d> / RI<d>, ten ranges x four points
    1   voltage limit                 RVL
    1   current limit                 RIL

Procedure model
---------------
The routine is **measurement-driven**. Each step selects a point, the operator's
DMM measures whatever the instrument actually puts out, and that measurement is
entered. No step assumes what a point *should* be, which is what makes the
routine safe even where the mapping between a selector and the manual's prose
description is uncertain: a mislabelled step still enters a self-consistent
value.

Nothing is committed until ``REND``. Abandoning a run at any point leaves the
stored calibration exactly as it was.

Bounds
------
Every point carries a plausibility window. This is not a substitute for the
accuracy specification -- it is a guard against a *silent hardware failure*
producing a number that looks like a measurement.

The failure mode it exists for is real and was observed on the bench, though in
the verification path rather than this one: driven by the ``D`` data command
with the voltage limit at its default VL130, the 1000 V range produces no
output at all, while its zero point still reads correctly. Range select and
OPERATE both appear healthy, and only a full-scale reading reveals anything is
wrong. An unguarded routine would have entered 0 as a 1000 V coefficient.

Any range that stops producing output for any reason fails the same way, since
a dead range and a correct zero are indistinguishable at the zero point. The
window is therefore applied to every point, not just the one that prompted it.

The window is set to the same 1 % of full scale that the firmware itself
enforces on ``RD``. That makes it do a second job: nothing is sent that the
instrument would refuse. A refused entry is silent -- every query is gated off
while EXT CAL is ENABLE -- so an unpredicted refusal used to leave the point at
its old coefficient with the operator's table showing the new value. The engine
also confirms every entry by serial poll, which is ungated; this window simply
avoids provoking the refusal in the first place.

Pairing
-------
Manual 7.2.2 requires zero and full scale to be calibrated as a pair, and the
reason is arithmetic rather than procedural: the zero coefficient is stored as a
fraction of the full-scale coefficient. Committing one without the other leaves
the range describing itself with one new number and one stale one. Each range
has two such pairs, one per polarity, and the three DAC sub-steps form a third
kind of indivisible group; :func:`unit_key` names them all, :func:`units`
collects them, and the engine measures each one whole before entering any of
it and refuses to commit a run in which any of them is half-entered.

Order
-----
The manual performs the DAC connection coefficients first, because the range
coefficients are measured through the DAC those coefficients describe. That
order is preserved. Within the remaining work the points are grouped by hookup
to keep the number of operator re-cablings to four; the manual's own order
places the voltage-limit step later, but each point is selected explicitly by
command so the ordering among independent coefficients does not matter.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from driver import (CURRENT_RANGES as _CURRENT_RANGES,
                    MANUAL_SHUNTS as _MANUAL_SHUNTS,
                    RANGES, RD_ACCEPTANCE_FRACTION, RKT_CEILING,
                   LIMIT_ENTRY_WINDOW, rd_accepts, rd_accepts_rkt,
                   rd_accepts_limit)

#: Fraction of full scale outside which a full-scale reading is rejected.
#:
#: This is deliberately **the same band the firmware enforces**, and not a
#: looser one. An earlier version used 5 %, reasoning that a wide window would
#: never reject a genuinely out-of-adjustment unit. That was the wrong trade:
#: ``RD`` refuses anything more than 1 % of full scale from the point's nominal
#: (the test at ``$5495``, raising Err.G19 at ``$53D4``), so a reading between
#: 1 % and 5 % passed our check, was sent, was thrown away by the instrument,
#: and still appeared in the operator's approval table as though it had been
#: stored. Accepting a value we know will be refused buys nothing.
#:
#: A unit that really is more than 1 % out cannot be adjusted by this procedure
#: at all -- the firmware will not take the numbers -- and saying so at the
#: point of measurement is more use than saying it after CAL END.
FS_WINDOW = RD_ACCEPTANCE_FRACTION

#: Fraction of full scale outside which a zero reading is rejected. Same band,
#: same reason: a zero point's nominal is zero, but its window is still 1 % of
#: the range's full scale.
ZERO_WINDOW = RD_ACCEPTANCE_FRACTION

#: Relative agreement required between two consecutive readings of the same
#: point before it is entered, as a fraction of the point's reference
#: magnitude, with an absolute floor for the very small ranges.
STABILITY_RELATIVE = 1e-4
STABILITY_FLOOR_SI = 2e-6


#: The manual's standard resistors, re-exported from :mod:`standards` so that a
#: verification and an adjustment cannot end up disagreeing about what the
#: Operation Manual specifies. See that module for which section names each one.
MANUAL_SHUNTS = _MANUAL_SHUNTS

#: The current ranges in the manual's calibration order, from the same source,
#: so that the 10 ohm is always last and the current-limit step can share its
#: hookup.
CURRENT_RANGE_ORDER = _CURRENT_RANGES


def _range_points(range_name: str) -> List[dict]:
    """Return the four calibration points of one range, in manual order.

    Manual 7.2.2 works each range as +Zero, +F.S, -Zero, -F.S, and states that
    zero and full scale must be re-calibrated as a pair -- redoing one means
    redoing the other -- so the four are always presented together.
    """
    spec = RANGES[range_name]
    selector = spec["sel"]
    out: List[dict] = []
    for point, suffix, fraction in (("+0", "+0", 0.0),
                                    ("+F.S", "+FS", 1.0),
                                    ("-0", "-0", 0.0),
                                    ("-F.S", "-FS", -1.0)):
        nominal_si = fraction * spec["fs_si"]
        out.append(dict(
            kind="range",
            label="%s %s" % (range_name, point),
            selector=selector + suffix,
            range=range_name,
            point=point,
            fraction=fraction,
            nominal_si=nominal_si,
            fs_si=spec["fs_si"],
            entry_unit=spec["unit"],
            entry_scale=spec["si"],       # SI per one entry unit
            measure=spec["kind"],          # "V" or "I"
            zero=(fraction == 0.0),
            hv=(range_name == "1000V"),
        ))
    return out


# --------------------------------------------------------------------------- #
# The DAC connection coefficients (manual 7.2.2, steps before the ranges)
#
# Three sub-steps, all performed on the 10 V range and all entered in volts:
#   1. zero output of the high-order digits
#   2. high-order digit at 1/10 full scale
#   3. low-order digits
#
# The firmware handlers ($8034, $8052, $806C) are identical apart from which of
# three consecutive slots they set, and they appear in the command table in the
# order A, B, D -- which is the order the manual performs the sub-steps in.
#
# Measured on a working instrument the three readings were 0.0127 V, 0.1128 V
# and 0.1138 V; subtracting the first from the other two gives 0.100084 V and
# 0.101087 V, an overlap of about 1 mV, which is the expected shape for a
# split-DAC connection measurement.
#
# These three do NOT use the ranges' +/-1 % band. An earlier version of this
# module assumed they did, on the strength of a single bench measurement that
# turned out to have been taken at RKTA only, and the assumption was refuted the
# first time it met a real reading: the instrument's own output at RKTB was
# 0.1128 V, which a +/-0.1 V band about zero would have refused -- making the
# manufacturer's own procedure impossible to complete.
#
# The real tests, traced in A05 ($52E8's RKT branch, which returns long before
# the range-point code at $5318):
#
#     RKTA  $53E6   |v| <= 0.100000, either sign
#     RKTB  $5406   v > 0, v > (the RKTA value just entered), |v| <= 0.200000
#     RKTD  $543F   v > 0, v > (the RKTB value just entered), |v| <= 0.200000
#
# The comparisons are against the values entered moments earlier in the same
# sequence, held at $C03A and $C042 -- not against anything stored. That is what
# makes the three a single indivisible unit, and it is also why they must be
# entered in the order A, B, D.
#
# The bench readings 0.0127 / 0.1128 / 0.1138 V satisfy all of it comfortably.
# --------------------------------------------------------------------------- #
DAC_POINTS: List[dict] = [
    dict(kind="free", label="DAC zero of high-order digits", selector="RKTA",
         entry_unit="V", entry_scale=1.0, measure="V",
         low=-0.1, high=0.1, reference=0.1, hv=False),
    dict(kind="free", label="DAC high-order digit at 1/10 F.S", selector="RKTB",
         entry_unit="V", entry_scale=1.0, measure="V",
         low=0.0, high=0.2, reference=0.2, hv=False),
    dict(kind="free", label="DAC low-order digits", selector="RKTD",
         entry_unit="V", entry_scale=1.0, measure="V",
         low=0.0, high=0.2, reference=0.2, hv=False),
]

#: Voltage-limit point. Manual 7.2.2(17): "Approximately 500 V voltage is
#: generated (and the HIGH V lamp lights)"; measure it and enter it in volts.
#:
#: The firmware's window is 400.0000 to 600.0000 V inclusive, and the entry must
#: be written with exactly three integer digits ($3F9A/$3FF7). The plausibility
#: window is set to match: outside it the point cannot be adjusted at all, so
#: there is nothing to be gained by measuring further out and then being
#: refused.
VOLTAGE_LIMIT_POINT: dict = dict(
    kind="free", label="Voltage limit (approximately 500 V)", selector="RVL",
    entry_unit="V", entry_scale=1.0, measure="V",
    low=400.0, high=600.0, reference=600.0, hv=True)

#: Current-limit point. Manual 7.2.2(19)-(20) uses the voltage-drop method: a
#: 10 ohm standard across the output terminals, the DMM measuring the drop
#: (approximately 0.5 V), and the *calculated current* entered in milliamps.
#: The manual's worked example is 0.5075 V / 10 ohm = 50.75 mA.
#:
#: The firmware accepts 40.00000 to 60.00000 mA, written with exactly two
#: integer digits. The plausibility window here is on the **measured voltage**,
#: so :func:`groups` derives it from the standard resistor actually in use
#: rather than assuming a nominal 10 ohm.
CURRENT_LIMIT_POINT: dict = dict(
    kind="free", label="Current limit (0.5 V across 10 ohm)",
    selector="RIL", entry_unit="mA", entry_scale=1.0, measure="V",
    low=0.4, high=0.6, reference=0.6, hv=False, shunt_ohm=10.0)


# --------------------------------------------------------------------------- #
# Hookup groups, in execution order
# --------------------------------------------------------------------------- #
def _shunt_range_points(range_name: str, resistance: float) -> List[dict]:
    """Return a current range's four points, measured as a voltage drop.

    The reference meter reads volts across a standard resistor instead of amps
    through its own current terminals. Only three fields change, and they change
    together: the point's SI quantities move into the measured domain (volts
    rather than amps) by multiplying by the resistance, and ``entry_scale`` is
    multiplied by the same factor so that ``entry_value`` still yields
    milliamps. Everything downstream -- the plausibility window, the stability
    tolerance, the meter range chosen, the RD acceptance prediction -- is
    expressed as a ratio of those fields and so needs no knowledge of any of it.
    """
    out = []
    for point in _range_points(range_name):
        point = dict(point)
        point["fs_si"] *= resistance
        point["nominal_si"] *= resistance
        point["entry_scale"] *= resistance
        point["measure"] = "V"
        point["shunt_ohm"] = resistance
        point["label"] += "  (%.6g ohm)" % resistance
        out.append(point)
    return out


def _limit_point(shunt_ohm: Optional[float]) -> dict:
    """Return the current-limit point, measured as a drop or measured directly.

    ``shunt_ohm`` is the calibrated value of the standard resistor the drop is
    taken across; a 10 milliohm error there is 1000 ppm on the current-limit
    coefficient, so an operator with a calibrated standard should supply its
    real value rather than the nominal 10 ohm.

    ``None`` measures the limit **directly**, with the meter's own current
    terminals in series. This is not the manual's method -- 7.2.2 has no direct
    equivalent for this step -- but it is the same measurement with one term
    fewer: the generator drives into what is very nearly a short, so it is in
    current limit, and the current through the meter *is* the limit current. No
    resistance is involved and none has to be known, and because the hookup is
    the direct-connect one the step costs no extra cabling.

    Either way the plausibility window is the firmware's own 40 to 60 mA,
    expressed in whatever the meter will be reading.
    """
    low_mA, high_mA, _, _ = LIMIT_ENTRY_WINDOW["RIL"]
    point = dict(CURRENT_LIMIT_POINT)
    if shunt_ohm is None:
        del point["shunt_ohm"]
        point["measure"] = "I"
        # SI per entry unit: the reading is in amps and RD wants milliamps.
        point["entry_scale"] = 1e-3
        point["low"] = low_mA / 1000.0
        point["high"] = high_mA / 1000.0
        point["label"] = ("Current limit (%.4g mA, direct read)"
                          % (0.05 * 1000.0))
    else:
        point["shunt_ohm"] = shunt_ohm
        point["low"] = low_mA / 1000.0 * shunt_ohm
        point["high"] = high_mA / 1000.0 * shunt_ohm
        point["label"] = ("Current limit (%.4g V across %.6g ohm)"
                          % (0.05 * shunt_ohm, shunt_ohm))
    point["reference"] = point["high"]
    return point


def groups(shunt_ohm: Optional[float] = None,
           shunts: Optional[dict] = None) -> List[dict]:
    """Return the adjustment groups, with their points.

    ``shunts`` selects how the three current *ranges* are measured, and may
    cover all of them, some of them or none:

    a range absent from ``shunts``
        Direct current measurement: the meter goes in series with the generator
        on its own current terminals. Every such range shares one group.
    a range present in ``shunts``
        The Operation Manual's voltage-drop method: the meter reads the drop
        across that range's standard resistor and the tool divides by it. The
        manual specifies a different resistor per range -- see
        :data:`MANUAL_SHUNTS` -- so each one is its own group.

    The two can therefore be mixed, because which is better is not a matter of
    taste but of what the operator owns. On a 3458A the direct current function
    is the weaker one, so at 100 mA the voltage-drop method roughly halves the
    reference uncertainty and at 1 mA with the manual's 1 kilohm it is better
    still -- but only for the ranges a resistor actually exists for.

    ``shunt_ohm`` is the resistor the current-limit step is measured across, or
    ``None`` to measure that step directly (see :func:`_limit_point`). It
    follows the 100 mA range, because the two share a hookup either way.
    """
    shunts = dict(shunts or {})
    # The current-limit step follows the 100 mA range, because the two share a
    # hookup either way. ``shunt_ohm`` is honoured only where the 100 mA range
    # has no resistor of its own, so the two can never end up disagreeing about
    # which method -- and therefore which unit -- that step is in.
    current_limit = _limit_point(shunts.get("100mA", shunt_ohm))
    direct_ranges = [name for name in CURRENT_RANGE_ORDER if name not in shunts]

    plan = [
        dict(
            key="main",
            title="Voltage Output",
            dmm_function="DCV",
            needs_thermal_wait=False,
            hookup=(
                "R6161 +OUTPUT  ->  DMM INPUT HI",
                "R6161 -OUTPUT  ->  DMM INPUT LO",
                "",
                "Two wires. Manual 7.2.2(1) calibrates with the DMM connected",
                "straight across the output terminals, on internal sense.",
                "",
                "WARNING: THIS GROUP ENERGISES THE TERMINALS TO 1000 V!",
                "The voltage-limit step generates approximately 500 V.",
            ),
            points=(DAC_POINTS
                    + _range_points("1V")
                    + _range_points("10V")
                    + _range_points("100V")
                    + _range_points("1000V")
                    + [VOLTAGE_LIMIT_POINT]),
        ),
        dict(
            key="divider",
            title="Divider Voltage Output",
            dmm_function="DCV",
            needs_thermal_wait=True,
            hookup=(
                "R6161 DIVIDER OUT HI  ->  DMM INPUT HI",
                "R6161 DIVIDER OUT LO  ->  DMM INPUT LO",
                "",
                "Please use low-EMF interconnects.",
                "",
                "A ONE MINUTE TIMER RUNS BEFORE THE FIRST READING, to let",
                "thermoelectric EMF at the new junctions settle while the output",
                "settles. At 10 mV full scale 1 microvolt = 100 ppm, larger than",
                "the quantity being measured, so it is part of the measurement.",
                "",
                "Low voltage only; no hazard in this group.",
            ),
            points=(_range_points("10mV")
                    + _range_points("100mV")
                    + _range_points("1000mV")),
        ),
    ]

    def _drop_hookup(resistance, extra=()):
        """The manual's four-terminal connection to a standard resistor."""
        return (
            "Manual 7.2.2, voltage-drop method:",
            "",
            "    +OUTPUT ---- I  [ %-9s ]  I ---- -OUTPUT" % ("%.6g ohm" % resistance),
            "                    V           V",
            "                    |           |",
            "               DMM INPUT HI   DMM INPUT LO",
            "",
            "Four terminals: the generator drives the resistor's CURRENT",
            "terminals and the meter reads its POTENTIAL terminals, so the",
            "lead resistance is outside the measurement. The tool converts",
            "the drop to current: I = V / R.",
        ) + tuple(extra)

    # The voltage-drop groups, in the manual's order. The 10 ohm comes last, so
    # that when the 100 mA range uses one the current-limit step can share that
    # hookup instead of demanding one of its own.
    for range_name in CURRENT_RANGE_ORDER:
        if range_name not in shunts:
            continue
        resistance = float(shunts[range_name])
        points = _shunt_range_points(range_name, resistance)
        share_limit = (range_name == "100mA")
        if share_limit:
            points = points + [current_limit]
        plan.append(dict(
            key="shunt_%s" % range_name,
            title="Current Output - Standard Resistor, %s range (%.6g ohm)"
                  % (range_name, resistance),
            dmm_function="DCV",
            needs_thermal_wait=False,
            hookup=_drop_hookup(
                resistance,
                ("", "Full scale puts about 1 V across this resistor.")
                + (("The current-limit step shares this hookup.",)
                   if share_limit else ())),
            points=points,
        ))

    # Everything no resistor was offered for is measured directly, in one group.
    # The current-limit step joins it when the 100 mA range did not claim it,
    # because it is the same connection -- so an operator with no resistors at
    # all makes three hookups for a whole adjustment rather than five.
    if direct_ranges or "100mA" not in shunts:
        points: List[dict] = []
        for range_name in direct_ranges:
            points.extend(_range_points(range_name))
        if "100mA" not in shunts:
            points.append(current_limit)
        covered = (", ".join(direct_ranges) if direct_ranges
                   else "current limit only")
        plan.append(dict(
            key="current",
            title="Current Output - Direct Connect (%s)" % covered,
            dmm_function="DCI",
            needs_thermal_wait=False,
            hookup=(
                "R6161 +OUTPUT  ->  DMM INPUT I  (current terminal)",
                "R6161 -OUTPUT  ->  DMM INPUT LO",
                "",
                "The DMM is now in series with the generator.",
                "",
                "NOTE -- A blown fuse reads as zero current, which resembles a",
                "dead output.",
            ) + (() if "100mA" in shunts else (
                "",
                "The current-limit step is measured on this hookup too: the",
                "meter is close enough to a short that the generator runs in",
                "current limit, so the reading IS the limit current.",
            )),
            points=points,
        ))
    return plan


def total_points(shunt_ohm: Optional[float] = None,
                 shunts: Optional[dict] = None) -> int:
    """Return the number of points in a complete adjustment (45, either way)."""
    return sum(len(g["points"]) for g in groups(shunt_ohm, shunts))


def stability_tolerance(point: dict) -> float:
    """Return the permitted spread between two readings of one point.

    The reference magnitude is the point's full scale for a range point, or the
    top of its plausibility window for a free point. A flat default would demand
    microvolt agreement on the 500 V limit step, which no instrument achieves.
    """
    if point["kind"] == "range":
        reference = abs(point["fs_si"])
    else:
        reference = abs(point.get("reference", 1.0))
    return max(STABILITY_RELATIVE * reference, STABILITY_FLOOR_SI)


def check_bounds(point: dict, measured_si: float) -> Tuple[bool, str]:
    """Validate a reading against the point's plausibility window.

    Returns ``(ok, reason)``. ``reason`` is empty when the reading is accepted
    and otherwise describes what was wrong, in terms the operator can act on.
    """
    if point["kind"] == "range":
        full_scale = abs(point["fs_si"])
        if point["zero"]:
            if abs(measured_si) > ZERO_WINDOW * full_scale:
                return False, ("zero reading %.9g exceeds %.3g, the %d %% of "
                               "full-scale window"
                               % (measured_si, ZERO_WINDOW * full_scale,
                                  int(ZERO_WINDOW * 100)))
            return True, ""
        target = point["nominal_si"]
        if target == 0.0:
            return True, ""
        ratio = abs(measured_si) / abs(target)
        if not (1.0 - FS_WINDOW) <= ratio <= (1.0 + FS_WINDOW):
            return False, ("reading %.9g is %.1f %% from the expected %.9g"
                           % (measured_si, (ratio - 1.0) * 100.0, target))
        if (measured_si < 0) != (target < 0):
            return False, "polarity is inverted (read %.9g)" % measured_si
        return True, ""

    low, high = point["low"], point["high"]
    if low <= measured_si <= high or low <= abs(measured_si) <= high:
        return True, ""
    return False, ("reading %.9g is outside the expected window %.3g to %.3g"
                   % (measured_si, low, high))


def will_rd_accept(point: dict, value_entry: float,
                   previous: Optional[float] = None) -> Tuple[bool, str]:
    """Predict whether ``RD`` will take ``value_entry`` at this point.

    Returns ``(ok, reason)``. Three different rules apply, one per point family,
    and they share nothing but the error they raise when they fail:

    * **Range points** -- inside 1 % of the range's decade nominal, with the
      right sign at full scale.
    * **DAC connection points** -- under a fixed ceiling and strictly increasing
      through A, B, D. ``previous`` is the value entered at the preceding
      sub-step, because that is exactly what the firmware compares against.
    * **Limit points** -- inside a fixed 400-600 V or 40-60 mA window, written
      with the right number of integer digits.

    This is a prediction, made so that a value the firmware is certain to refuse
    is never sent: a refusal costs a retry, lights Err.G19 on the panel, and --
    since it leaves the point at its old coefficient -- would otherwise be
    indistinguishable from success. It does not replace the serial-poll
    confirmation the engine takes after every entry; it just means the poll
    should never have anything to report.
    """
    if point["kind"] == "range":
        range_name = point["range"]
        nominal = rd_nominal(point)
        if rd_accepts(range_name, nominal, value_entry):
            return True, ""
        band = RD_ACCEPTANCE_FRACTION * RANGES[range_name]["fs"]
        return False, ("RD will refuse %.9g with Err.G19: the firmware accepts "
                       "%.9g +/- %.9g at this point (1 %% of the %s range's "
                       "full scale, from the ROM constant at $9C85+) and this "
                       "is %.9g outside"
                       % (value_entry, nominal, band, range_name,
                          abs(value_entry - nominal) - band))

    selector = point["selector"]
    if selector in RKT_CEILING:
        ok, reason = rd_accepts_rkt(selector, value_entry, previous)
        if ok:
            return True, ""
        return False, "RD will refuse this entry with Err.G19: %s" % reason
    if selector in LIMIT_ENTRY_WINDOW:
        ok, reason = rd_accepts_limit(selector, value_entry)
        if ok:
            return True, ""
        return False, "RD will refuse this entry with Err.G19: %s" % reason
    return True, ""


def rd_nominal(point: dict) -> float:
    """Return the value the firmware compares an entry against, in entry units.

    Only meaningful for range points; the special points do not have one.
    """
    if point["kind"] == "range":
        return point["fraction"] * point["fs_si"] / point["entry_scale"]
    return 0.0


def unit_key(point: dict) -> str:
    """Return the name of the all-or-nothing group this point belongs to.

    Some coefficients cannot be entered independently, and entering part of one
    is worse than entering none of it, because a partial entry is committed
    alongside stale companions and nothing on the instrument says so.

    Three kinds of unit exist:

    * **A range's zero/full-scale pair, per polarity.** Manual 7.2.2 says zero
      and full scale must be re-calibrated together, and the storage format is
      why: the zero coefficient is held as a *fraction of* the full-scale
      coefficient. A new zero against an old full scale is a wrong number, not
      an old one. ``1V +`` and ``1V -`` are separate units.
    * **The three DAC connection entries, together.** RKTA, RKTB and RKTD are
      not three coefficients; the firmware combines them into one value at
      ``$4302``/``$433B``/``$435F``, which ``$0C5E``-``$0C64`` calls as a unit,
      from ``|B - A|`` and ``|D - B|``. Two fresh entries against one stale one
      produce a coefficient that was never measured.
    * **Each limit point alone**, which really is a single independent record.

    Both zero points carry ``fraction`` 0.0, so the sign of the fraction cannot
    tell ``+0`` from ``-0``; the point name can, and its first character is the
    polarity.
    """
    if point["kind"] == "range":
        return "%s %s" % (point["range"], point["point"][0])
    if point["selector"] in ("RKTA", "RKTB", "RKTD"):
        return "DAC connection"
    return point["label"]


def units(points: Sequence[dict]) -> List[Tuple[str, List[dict]]]:
    """Group points into their all-or-nothing units, preserving order.

    The catalogue already lists each unit's members consecutively, so the
    returned order is the order the points would have been worked in anyway.
    """
    order: List[str] = []
    buckets: Dict[str, List[dict]] = {}
    for point in points:
        key = unit_key(point)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(point)
    return [(key, buckets[key]) for key in order]


def entry_value(point: dict, measured_si: float) -> float:
    """Convert a reading into the number ``RD`` expects for this point.

    Range points are entered in the range's panel unit (so the 1 V range is
    entered in millivolts). The DAC and voltage-limit points are entered in
    volts. The current-limit point is entered in milliamps, either from a
    voltage measured across the standard resistor or, where there is no
    resistor, from the limit current read directly.
    """
    if point["kind"] == "range":
        # For a shunt-measured current range ``entry_scale`` already carries the
        # resistance, so this one division does both conversions: volts to amps
        # and amps to the range's entry unit.
        return measured_si / point["entry_scale"]
    if "shunt_ohm" in point:
        return measured_si / point["shunt_ohm"] * 1000.0
    # A free point measured directly. ``entry_scale`` is SI per entry unit, so
    # it is 1.0 for the volt-entered points and 1e-3 for a current-limit step
    # read in amps and entered in milliamps.
    return measured_si / point.get("entry_scale", 1.0)
