"""Performance-verification test plan for the Advantest R6161.

The Operation Manual publishes an accuracy specification (section 8.1) but no
performance-test record, so the point list here is derived rather than
transcribed: it exercises each range at the points the calibration procedure
itself adjusts, which is the smallest set that can demonstrate conformance to
the specification at both polarities and at zero.

Points are graded by :mod:`spec`, so the limits are never duplicated here;
this module decides *what* is measured and the specification decides *whether it
passed*.

Hookup groups
-------------
The R6161 cannot present all ten ranges on one pair of terminals, so both the
verification and the adjustment are organised into groups that share a
connection. The operator is prompted once per group:

``main``     +OUTPUT / -OUTPUT to the DMM voltage input -- 1 V, 10 V, 100 V, 1000 V
``divider``  the separate DIVIDER OUTPUT terminals -- 10 mV, 100 mV, 1000 mV
``current``  the DMM current terminals, in series -- 1 mA, 10 mA, 100 mA

The Divider grouping is not a convenience: Table 4-3 of the manual footnotes
that V2, V3 and V9 are Divider ranges, and manual 7.2.2 directs the operator to
"use a copper clip for cable connection to the divider output terminals (Hi and
Lo)". Measuring them on the main output leads reads approximately zero.

The current group has two shapes, chosen by the operator before the run starts
and passed to :func:`plan` as ``shunts``. Either the meter measures the current
directly on its own terminals -- one group, one hookup -- or it measures the
drop across a standard resistor and the tool divides by the resistance, which
is the Operation Manual's own method and needs a different resistor per range,
so it becomes three groups. A verification and an adjustment must be able to
make the same choice, or an as-left certificate would be traceable to different
standards from the adjustment it certifies.

All values are stored in SI base units (volts, amps). A shunt point is no
exception: ``setting_si`` stays in amps, because that is what the generator is
commanded to produce. Only the *measurement* moves into the voltage domain, and
:func:`measured_si` is what converts between the two.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from driver import RANGES, CURRENT_RANGES

# --------------------------------------------------------------------------- #
# Safe output limits.
#
# These are the instrument's own initial values (Operation Manual 4.5.3): the
# voltage limit defaults to 130 V and the current limit to 125 mA. The engine
# restores them when a verification finishes, so an elevated limit is never
# left armed for whoever uses the instrument next.
# --------------------------------------------------------------------------- #
SAFE_VOLTAGE_LIMIT = 130
SAFE_CURRENT_LIMIT = 125

# --------------------------------------------------------------------------- #
# Hookup groups
#
# ``dmm_function`` selects which DMM measurement the engine configures:
# ``"DCV"`` for the voltage groups, ``"DCI"`` for the current group.
# ``needs_thermal_wait`` marks a group whose hookup disturbs thermoelectric
# junctions. The engine enforces ``r6161.THERMAL_WAIT_S`` before that group's
# first reading; the length is a user setting, so it is not baked in here.
#
# ``voltage_limit`` / ``current_limit`` are what this group needs, raised just
# before it runs and dropped back afterwards. Only the main group needs
# anything unusual: at the default VL130 the 1000 V range produces no output at
# all when driven by the ``D`` command. The Divider group is limited to about
# 1.2 V and the current group needs only the 10 V of compliance the
# specification assumes, so both are content with the safe defaults.
#
# Note that limits cannot be set at all while a Divider range is selected
# (Err.G16), which is why the engine selects a non-Divider range first.
# --------------------------------------------------------------------------- #
GROUPS: List[dict] = [
    dict(
        key="main",
        title="Voltage Output",
        dmm_function="DCV",
        ranges=("1V", "10V", "100V", "1000V"),
        needs_thermal_wait=False,
        voltage_limit=1250,
        current_limit=125,
        hookup=(
            "R6161 +OUTPUT  ->  DMM INPUT HI",
            "R6161 -OUTPUT  ->  DMM INPUT LO",
            "",
            "Two wires. Manual 7.2.2(1) calibrates with the DMM connected",
            "straight across the output terminals, on internal sense.",
            "",
            "WARNING: THIS GROUP ENERGISES THE TERMINALS TO 1000 V!",
        ),
    ),
    dict(
        key="divider",
        title="Divider Voltage Output",
        dmm_function="DCV",
        ranges=("10mV", "100mV", "1000mV"),
        needs_thermal_wait=True,
        voltage_limit=SAFE_VOLTAGE_LIMIT,
        current_limit=SAFE_CURRENT_LIMIT,
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
    ),
    dict(
        key="current",
        title="Current Output - Direct Connect",
        dmm_function="DCI",
        ranges=("1mA", "10mA", "100mA"),
        needs_thermal_wait=False,
        voltage_limit=SAFE_VOLTAGE_LIMIT,
        current_limit=SAFE_CURRENT_LIMIT,
        hookup=(
            "R6161 +OUTPUT  ->  DMM INPUT I  (current terminal)",
            "R6161 -OUTPUT  ->  DMM INPUT LO",
            "",
            "The DMM is now in series with the generator.",
            "",
            "NOTE -- A blown fuse reads as zero current, which resembles a",
            "dead output.",
        ),
    ),
]

# --------------------------------------------------------------------------- #
# Points measured on every range, as a fraction of full scale.
#
# Zero is included because the specification's range-error term is what bounds
# it, and because a zero-offset fault is invisible in a full-scale-only test:
# a wiped set of zero coefficients moves every range by a constant fraction of
# full scale while the gain still looks correct.
#
# The mid-scale points are not required to demonstrate conformance but are cheap
# and reveal linearity problems that the endpoints cannot.
# --------------------------------------------------------------------------- #
FRACTIONS: Tuple[Tuple[str, float], ...] = (
    ("+F.S", 1.0),
    ("+50%", 0.5),
    ("Zero", 0.0),
    ("-50%", -0.5),
    ("-F.S", -1.0),
)

#: A shorter list for the quick verification, which measures only the points the
#: adjustment procedure itself sets.
FRACTIONS_SHORT: Tuple[Tuple[str, float], ...] = (
    ("+F.S", 1.0),
    ("Zero", 0.0),
    ("-F.S", -1.0),
)


def points_for(range_name: str, short: bool = False,
               shunt_ohm: Optional[float] = None) -> List[dict]:
    """Return the verification points for one range.

    Each point carries the commanded value in SI base units and in the range's
    panel unit, plus the full scale, so the engine and the report never have to
    recompute the conversion.

    ``shunt_ohm`` marks a current range that will be measured as a voltage drop.
    It changes nothing about what is commanded -- the generator is still asked
    for amps -- and only records what the reading will have to be divided by.
    """
    spec = RANGES[range_name]
    fractions = FRACTIONS_SHORT if short else FRACTIONS
    out: List[dict] = []
    for label, fraction in fractions:
        point = dict(
            range=range_name,
            point=label,
            fraction=fraction,
            setting_si=fraction * spec["fs_si"],
            setting_panel=fraction * spec["fs"],
            unit=spec["unit"],
            fs_si=spec["fs_si"],
            kind=spec["kind"],
        )
        if shunt_ohm:
            point["shunt_ohm"] = float(shunt_ohm)
        out.append(point)
    return out


def measured_si(point: dict) -> float:
    """The magnitude the reference meter will actually see at this point.

    The same as the commanded value everywhere except on a shunt point, where
    the meter reads volts across a resistor rather than the amps through it.
    Used to pick the meter's range and to check it against the meter's input
    ceiling -- both questions about what arrives at the terminals, not about
    what the generator was asked for.
    """
    return point["setting_si"] * point.get("shunt_ohm", 1.0)


def _drop_hookup(resistance: float, extra: Tuple[str, ...] = ()) -> Tuple[str, ...]:
    """The manual's four-terminal connection to a standard resistor."""
    return (
        "Manual 7.2.2, voltage-drop method:",
        "",
        "    +OUTPUT ---- I  [ %-9s ]  I ---- -OUTPUT"
        % ("%.6g ohm" % resistance),
        "                    V           V",
        "                    |           |",
        "               DMM INPUT HI   DMM INPUT LO",
        "",
        "Four terminals: the generator drives the resistor's CURRENT",
        "terminals and the meter reads its POTENTIAL terminals, so the",
        "lead resistance is outside the measurement. The tool converts",
        "the drop to current: I = V / R.",
    ) + tuple(extra)


def _shunt_groups(shunts: dict, short: bool = False) -> List[dict]:
    """One hookup group per standard resistor, in the manual's order.

    ``shunts`` may cover only some of the current ranges. The ranges it does not
    cover keep the direct-connect group, which :func:`plan` appends after these
    -- so an operator with one resistor measures one range as a drop and the
    other two through the meter, rather than having to choose one method for all
    three.
    """
    out: List[dict] = []
    for range_name in CURRENT_RANGES:
        if range_name not in shunts:
            continue
        resistance = float(shunts[range_name])
        out.append(dict(
            key="shunt_%s" % range_name,
            title="Current Output - Standard Resistor, %s range (%.6g ohm)"
                  % (range_name, resistance),
            dmm_function="DCV",
            ranges=(range_name,),
            needs_thermal_wait=False,
            voltage_limit=SAFE_VOLTAGE_LIMIT,
            current_limit=SAFE_CURRENT_LIMIT,
            hookup=_drop_hookup(
                resistance,
                ("", "Full scale puts about 1 V across this resistor.")),
            points=points_for(range_name, short=short, shunt_ohm=resistance),
        ))
    return out


def plan(group_keys: Tuple[str, ...] = (), short: bool = False,
         shunts: Optional[dict] = None) -> List[dict]:
    """Return the full verification plan, optionally restricted to some groups.

    The result is a list of group dicts, each with a ``points`` list, so the
    engine can walk groups (prompting for a hookup once each) and then points.

    ``shunts`` replaces the direct-measurement current group with one group per
    standard resistor named in it. A range absent from ``shunts`` stays in the
    direct group, so the two methods can be mixed: with a 10 ohm resistor and
    nothing else, 100 mA is measured as a voltage drop and 1 mA and 10 mA
    through the meter's own current terminals. ``group_keys`` filters on the
    keys that result, so ``("current",)`` selects the direct group and
    ``("shunt_1mA",)`` one of the drop groups.
    """
    out: List[dict] = []
    for group in GROUPS:
        ranges = group["ranges"]
        if shunts and group["key"] == "current":
            out.extend(entry for entry in _shunt_groups(shunts, short=short)
                       if not group_keys or entry["key"] in group_keys)
            # Whatever the resistors did not cover is still measured directly.
            ranges = tuple(name for name in ranges if name not in shunts)
            if not ranges:
                continue
        if group_keys and group["key"] not in group_keys:
            continue
        entry = dict(group)
        entry["ranges"] = ranges
        if ranges != group["ranges"]:
            # A partial direct group: say which ranges are on this hookup, so a
            # mixed run's headings account for all three between them.
            entry["title"] = "%s (%s)" % (group["title"], ", ".join(ranges))
        points: List[dict] = []
        for range_name in ranges:
            points.extend(points_for(range_name, short=short))
        entry["points"] = points
        out.append(entry)
    return out


def total_points(group_keys: Tuple[str, ...] = (), short: bool = False,
                 shunts: Optional[dict] = None) -> int:
    """Return how many readings a plan will take, for the progress display."""
    return sum(len(g["points"]) for g in plan(group_keys, short=short,
                                              shunts=shunts))


# --------------------------------------------------------------------------- #
# Display scaling
# --------------------------------------------------------------------------- #
def display_value(value_si: float, kind: str) -> Tuple[float, str]:
    """Scale an SI value into a friendly engineering unit.

    ``kind`` is ``"V"`` or ``"I"``. Returns ``(scaled_value, unit_string)``.
    """
    magnitude = abs(value_si)
    if kind == "I":
        if magnitude >= 1e-3 or magnitude == 0.0:
            return value_si / 1e-3, "mA"
        if magnitude >= 1e-6:
            return value_si / 1e-6, "uA"
        return value_si / 1e-9, "nA"
    if magnitude >= 1.0 or magnitude == 0.0:
        return value_si, "V"
    if magnitude >= 1e-3:
        return value_si / 1e-3, "mV"
    return value_si / 1e-6, "uV"
