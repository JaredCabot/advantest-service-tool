"""The parts of running a procedure that a verification and an adjustment share.

A verification measures and grades; an adjustment measures and enters. Between
those two acts they are the same procedure, and every place where they were not
was an accident rather than a decision:

* both walk hookup groups, show the connection, and ask whether to run one
* both wait for the output to settle, and for thermoelectric EMF where the
  hookup disturbs it
* both must leave the instrument in the same state afterwards, because it is
  the same instrument and the same next operator

Keeping those here means a change to any of them cannot reach one procedure and
miss the other, which is how the two came to word the same prompt differently
and to disagree about where sense and guard should be left.

The estimator is here for the same reason. The menu quotes how long each
procedure takes, and a figure typed into a menu goes stale the first time
somebody changes a settling time. This one is derived from the plan and the
settings actually in force.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

import active
import units
from i18n import t


# --------------------------------------------------------------------------- #
# One hookup group
# --------------------------------------------------------------------------- #
def announce_group(console, gen, group: dict) -> str:
    """Show a group's hookup and ask whether to run it.

    Returns ``"run"``, ``"skip"`` or ``"exit"``.

    The heading, the connection and the choice are one thing on the screen, and
    the choice comes last: an operator answers a question about a group after
    reading what the group needs, not before it. There is no separate
    "connected and ready?" any more, because [R]un is that answer -- asking
    twice trained the operator to press Enter through both.
    """
    try:
        gen.standby()
    except Exception:
        pass
    print("")
    console.hr()
    print(" " + group["title"])
    console.hr()
    for line in group["hookup"]:
        print("   " + line)
    print("")
    print(t("   The output is in STANDBY. Make the connection now."))
    return console.section_choice(group["title"])


# --------------------------------------------------------------------------- #
# Settling
# --------------------------------------------------------------------------- #
def settle_seconds(point: dict) -> float:
    """Return the settling time one point needs after OPERATE.

    Three figures, all the manual's own: the general one, the longer 1000 V one
    from the footnote against those steps, and the minute the manual asks for on
    the 100 mA range and nowhere else. Written to suit both point shapes -- an
    adjustment point marks the high-voltage steps with ``hv`` because the
    voltage-limit step is one and belongs to no range.
    """
    if point.get("hv") or point.get("range") == "1000V":
        return active.driver.SETTLE_HV_S
    if point.get("range") == "100mA":
        return active.driver.SETTLE_100MA_S
    return active.driver.SETTLE_S


def settle_for(point: dict,
               pending: Optional[List[float]] = None) -> Tuple[float, str]:
    """Return ``(seconds, label)`` to wait after energising one point.

    ``pending`` is a one-element list holding a group's thermoelectric
    allowance, and it is *consumed*: the first point of the group waits the
    longer of the two, and every point after it waits its own settling time.

    The allowance used to be a separate wait taken before the group, with the
    output still in STANDBY. That settled the junctions and nothing else, and
    the first point then waited its own ten seconds on top -- a minute of
    settling nothing followed by the only settling that counted. Run together
    they overlap, which is what they physically do anyway.
    """
    seconds = settle_seconds(point)
    label = "settling"
    if pending:
        if pending[0] > seconds:
            seconds, label = pending[0], "thermal + settling"
        pending[0] = 0.0
    return seconds, label


def thermal_allowance(console, group: dict) -> List[float]:
    """Announce a group's thermoelectric allowance and return it, ready to use.

    At 10 mV full scale a 1 microvolt offset at a freshly made junction is
    100 ppm, larger than the tolerance the reading is graded against, so this is
    a measurement requirement and not advice.
    """
    seconds = active.driver.THERMAL_WAIT_S if group.get("needs_thermal_wait") else 0.0
    if seconds:
        print("")
        print(t("   The first reading is taken after %d s, to let "
                "thermoelectric") % seconds)
        print(t("   EMF at the new junctions settle while the output settles."))
    return [float(seconds)]


# --------------------------------------------------------------------------- #
# Leaving the instrument
# --------------------------------------------------------------------------- #
def restore_initial_state(gen, announce: bool = True) -> None:
    """Leave the generator as Table 3-1 says it leaves the factory.

    Standby first, so the terminals are de-energised before anything else is
    attempted, then EXT-SENSE and EXT-GUARD OFF. Each step is independent: a
    failure to restore sense must not leave the output live.

    Both procedures switch to internal sense because manual 7.2.2(1) measures
    straight across the output terminals, and both used to switch back to
    EXTERNAL afterwards on the grounds that it was the operator's preference. It
    was not; it was this tool's guess. Table 3-1 (manual 3.1.3, page 3-3) gives
    the initial setup status as EXT-GUARD OFF and EXT-SENSE OFF, and lists both
    among the parameters that are **backed up** -- so whatever a run leaves
    behind survives the next power cycle, and an instrument silently left
    sensing through somebody else's leads explains nothing about itself on the
    front panel.
    """
    for action, description in (
            (gen.standby, "output to STANDBY"),
            (lambda: gen.set_sense(active.driver.INITIAL_EXT_SENSE), "EXT-SENSE OFF"),
            (lambda: gen.set_guard(active.driver.INITIAL_EXT_GUARD), "EXT-GUARD OFF")):
        try:
            action()
        except Exception as exc:
            print(t("   could not set %s (%s)") % (description, exc))
    if announce:
        print(t("Output in STANDBY; EXT-SENSE and EXT-GUARD OFF."))


# --------------------------------------------------------------------------- #
# How long a procedure takes
# --------------------------------------------------------------------------- #
def estimated_minutes(groups: Sequence[dict], extra_s: float = 0.0) -> int:
    """Estimate a procedure's wall clock, in whole minutes.

    Derived from the plan and from the settling times actually in force, so a
    figure shown on the menu cannot contradict the settings shown under
    *Instrument information*. Rounded up to the nearest five minutes, because
    quoting a single minute would claim a precision this does not have: the
    per-reading allowance is a 3458A at 100 PLC, and a faster meter finishes
    early.
    """
    total = float(extra_s)
    for group in groups:
        total += active.driver.HOOKUP_OVERHEAD_S
        pending = [active.driver.THERMAL_WAIT_S
                   if group.get("needs_thermal_wait") else 0.0]
        for point in group.get("points", ()):
            seconds, _ = settle_for(point, pending)
            total += seconds + active.driver.POINT_OVERHEAD_S
    minutes = total / 60.0
    return int(5 * round((minutes + 2.4) / 5.0)) or 5
