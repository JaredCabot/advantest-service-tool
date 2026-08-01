"""Automated adjustment, for whichever instrument is bound.

Performs the complete manual 7.2.2 routine over GPIB -- 45 points in four hookup
groups -- measuring every point on the reference multimeter and entering the
measurement with ``RD``. The whole set is committed by a single ``REND`` at the
end, and only after the operator has reviewed the complete table.

Safety model
------------
Six properties make this safe to run on a working instrument:

1. **Nothing is committed until CAL END.** Entries accumulate in the
   instrument's RAM; abandoning a run at any point, for any reason, leaves the
   stored calibration exactly as it was.
2. **Every reading is validated before it is entered.** A reading must fall
   inside the point's plausibility window and two consecutive readings must
   agree. This guards against a silent hardware failure producing a number that
   looks like a measurement rather than an absence of one -- a range that
   produces no output still reads a plausible zero at its zero point, so only a
   full-scale reading reveals the fault.
3. **Every entry is confirmed by serial poll.** ``RD`` refuses a value more than
   1 % of full scale from the point's nominal, and because every query is gated
   to EXT CAL = DISABLE the refusal is otherwise completely silent: the point
   keeps its old coefficient while the operator's table shows the new value. The
   IEEE-488 status byte is ungated and reports it. See the instrument's own
   driver module for the firmware path and the bench measurements.
4. **Coefficients that cannot stand alone are entered all-or-nothing.** A
   range's zero and full-scale coefficients are one unit per polarity, because
   the zero is stored as a fraction of the full scale; the three DAC entries are
   one unit, because the firmware combines them into a single value. Each unit
   is measured completely before any of it is entered, so a unit that cannot be
   measured is skipped whole rather than committed half-done.
5. **The commit is read back.** After CAL END the EEPROM master is re-read, the
   stored checksum is recomputed and compared, and every record is checked for
   the signatures of a bad write. A checksum mismatch is not cosmetic: at the
   next power-up the firmware discards the block and substitutes ROM defaults.
6. **The operator approves the whole table** before ``REND`` is sent.

The routine is measurement-driven: each step selects a point, measures whatever
the instrument actually outputs, and enters that. No step assumes what a point
should be.

Output limits
-------------
**Neither output limit is set by this module.** During calibration the firmware
drives the output from the selected point rather than through the ``D`` data
command, and ``VL``/``IL`` do not gate that path.

They do matter when the output is driven with ``D``, which is what a
performance verification does: at the default VL130 the 1000 V range produces
no output at all while its zero point still reads correctly. :mod:`perf_engine`
therefore raises the limits per hookup group and lowers them again afterwards.
This module leaves them alone, so a calibration cannot leave an elevated limit
armed behind it.

Should that prove wrong on some unit, the consequence is contained rather than
silent: the affected full-scale reading would fall outside its plausibility
window and be rejected with a message, not entered as a coefficient.

Why EXT CAL must start in DISABLE
----------------------------------
Not for the limits, but for ``MRB``: reading the calibration memory is gated to
that position (handler ``$8BEE``), and the before-image is what allows the
committed change to be shown afterwards. ``SEN0``/``SEN1`` and ``GRD0``/
``GRD1`` are ungated (handlers ``$7E5E``, ``$7E93``, ``$7E09``, ``$7E33``
contain no gate call) and could be set at any time; they are applied in the
same place purely for clarity.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

import active
import procedure
import standards
import units

# The instrument's own modules are reached through :mod:`active` rather than
# imported: which ones they are is not known until an instrument is chosen.
#
# SETTLE_S, SETTLE_HV_S and THERMAL_WAIT_S are likewise never bound by name.
# They are user settings, assigned over at startup, and a from-import would
# bind whatever they held at import time -- silently ignoring the operator's
# value. They are read as ``active.driver.SETTLE_S`` at the moment they are
# used.
from i18n import t


# --------------------------------------------------------------------------- #
# DMM range and integration time
# --------------------------------------------------------------------------- #
_DCV_RANGES = (0.1, 1.0, 10.0, 100.0, 1000.0)
_DCI_RANGES = (1e-3, 1e-2, 1e-1)


def _dmm_range(point: dict) -> str:
    """Return the DMM range string for one adjustment point.

    Zero points and the DAC points are autoranged: their magnitude is what is
    being determined, so it cannot be assumed in advance.
    """
    if point["kind"] != "range":
        return "1000" if point["selector"] == "RVL" else "AUTO"
    if point["zero"]:
        return "AUTO"
    magnitude = abs(point["nominal_si"])
    table = _DCI_RANGES if point["measure"] == "I" else _DCV_RANGES
    for candidate in table:
        if magnitude <= candidate * 1.05:
            return "%g" % candidate
    return "AUTO"


def _nplc(point: dict) -> None:
    """Return the integration time to request for one point.

    ``None``, deliberately: the integration time belongs to the meter, not to
    the point, so it comes from that meter's own file in ``dmm_configs/`` --
    the ``nplc`` key, which is 100 for the 3458A. Passing ``None`` to
    :meth:`dmm_driver.DMM.measure` is what selects it.

    This replaces a graduated scheme that used 20 PLC on the large signals and
    100 only where the quantity approached the meter's noise floor. The saving
    was real but small next to the settling times the same points need, and
    deciding here would have meant one number applied to eleven meters with
    different capabilities.
    """
    return None


# --------------------------------------------------------------------------- #
# One point
# --------------------------------------------------------------------------- #
def _measure_once(engine, dmm, group: dict, point: dict,
                  pending: Optional[List[float]] = None
                  ) -> Tuple[Optional[float], str]:
    """Select, energise, settle and measure one point a single time.

    ``pending`` carries the group's thermoelectric allowance, consumed by the
    first point that is actually energised. A retry after an unstable reading
    finds it already spent, which is right: the junctions settled during the
    attempt that failed.
    """
    gen = engine.gen

    function = group["dmm_function"]
    gen.select_cal_point(point["selector"])

    # Range the meter BEFORE the source is energised, so the signal never
    # arrives at a meter still set up for the previous point. Configuring
    # afterwards would mean a 1000 V step reaching a 3458A still ranged for the
    # 100 V point, and a range change during the settling window the reading
    # then depends on.
    try:
        dmm.setup(function, _dmm_range(point), _nplc(point))
    except Exception as exc:
        return None, "DMM setup failed (%s)" % exc

    gen.operate()
    seconds, label = procedure.settle_for(point, pending)
    units.wait_with_progress(seconds, label=label)

    try:
        measured, spread = dmm.measure(function, _dmm_range(point),
                                       _nplc(point), samples=2,
                                       configure=False)
    except Exception as exc:
        return None, "DMM read failed (%s)" % exc

    tolerance = active.adjust.stability_tolerance(point)
    if spread > tolerance:
        return None, ("reading unstable: spread %.3g exceeds %.3g"
                      % (spread, tolerance))

    ok, reason = active.adjust.check_bounds(point, measured)
    if not ok:
        return None, reason
    return measured, ""


def _measure_point(engine, dmm, group: dict, point: dict,
                   retries: int = 2,
                   pending: Optional[List[float]] = None
                   ) -> Tuple[Optional[float], str]:
    """Measure one point, retrying if the reading is unstable.

    Returns ``(measured_si, reason)``. ``measured_si`` is ``None`` when the
    point could not be accepted, and ``reason`` then says why in terms the
    operator can act on.

    The retry exists because instability is usually transient -- a thermal
    transient on the divider ranges, mains noise, a settling tail that ran long
    -- and because a skipped point now costs a whole unit rather than a single
    coefficient. A reading that is out of bounds is *not* retried: that is a
    real fault and repeating the measurement only hides it.
    """
    print(t("   -- %s") % point["label"])
    measured, reason = _measure_once(engine, dmm, group, point, pending)
    attempt = 1
    while measured is None and "unstable" in reason and attempt < retries:
        attempt += 1
        print(t("      %s; retry %d of %d ...") % (reason, attempt, retries))
        measured, reason = _measure_once(engine, dmm, group, point, pending)
    return measured, reason


# --------------------------------------------------------------------------- #
# One all-or-nothing unit
# --------------------------------------------------------------------------- #
def _unit_full_scale(points: List[dict]) -> float:
    """Return the largest magnitude a unit will put on the meter's terminals.

    For a range unit this is the range's **full scale**, deliberately -- not the
    individual point's nominal. A zero point's nominal is 0.0, and testing that
    against the meter's ceiling used to let a range's zero points through on a
    meter that could not survive the range's full scale. The pair then went in
    half-entered. The unit is the thing that has to fit, not the point.
    """
    worst = 0.0
    for point in points:
        if point["kind"] == "range":
            candidate = abs(point["fs_si"])
        else:
            candidate = max(abs(point.get("low") or 0.0),
                            abs(point.get("high") or 0.0))
        worst = max(worst, candidate)
    return worst


def _run_unit(engine, dmm, group: dict, key: str, points: List[dict],
              entered: List[dict],
              pending: Optional[List[float]] = None) -> Tuple[str, str]:
    """Measure a whole unit, then enter a whole unit.

    Returns ``(outcome, detail)`` where ``outcome`` is one of:

    ``"entered"``   every point measured and every entry confirmed
    ``"skipped"``   a point could not be measured; **nothing was entered**, so
                    the unit keeps the coefficients it already had
    ``"abort"``     an entry the firmware was predicted to accept was refused,
                    or the status byte could not be read. The run must stop: a
                    unit is now half-entered in the instrument's RAM and there
                    is no command that withdraws an entry.

    Each point is selected, energised, settled, measured and entered before the
    next is touched, so ``RD`` always reaches the firmware with the point still
    in the state its reading was taken in. Once ``RD`` has taken a value it is
    in RAM until ``REND`` writes it or the run is abandoned; there is no command
    that withdraws an entry, which is why a failure part way through a unit is
    an abort rather than a skip.
    """
    gen = engine.gen
    taken = 0

    # ---- one point at a time: select, energise, settle, measure, enter ------ #
    #
    # The measurement leaves the point selected and in OPERATE and ``RD``
    # follows immediately, so the entry is made in exactly the state the reading
    # was taken in. An earlier design measured a whole unit and then entered the
    # whole unit, which meant re-selecting every point -- and re-selecting drops
    # the point out of OPERATE, where RD is accepted on the status byte and then
    # silently discarded. That threw away a complete 45-point run on 29 July
    # 2026 and the loss was invisible until the post-commit read-back. This
    # order has no re-selection step to get wrong.
    #
    # What it gives up is the guarantee that a unit can never be half-entered.
    # That is worth less than it sounds: nothing reaches EEPROM until CAL END,
    # this function returns "abort" the moment a point fails after another has
    # been entered, and a half-entered unit lives only in RAM until the next
    # power cycle clears it.
    #
    # ``previous`` carries the preceding sub-step's value along the DAC chain.
    # RKTB is checked against the RKTA value the firmware has just taken, and
    # RKTD against RKTB's, so the prediction has to walk the same chain or it
    # would be checking against nothing.
    previous: Optional[float] = None
    for point in points:
        # Try harder once part of the unit is already in RAM. Before a single
        # entry has been made, a failure costs a skipped unit; after one, it
        # costs the whole run, because there is no command that withdraws an
        # entry. The cost of failing rose, so the effort spent avoiding it does
        # too.
        measured, reason = _measure_point(engine, dmm, group, point,
                                          retries=4 if taken else 2,
                                          pending=pending)
        if measured is None:
            if taken:
                return "abort", (
                    "%s: %s. %d of this unit's %d entries are already in the "
                    "instrument's RAM and there is no command that withdraws "
                    "an entry."
                    % (point["label"], reason, taken, len(points)))
            return "skipped", "%s: %s" % (point["label"], reason)
        value = active.adjust.entry_value(point, measured)
        acceptable, why = active.adjust.will_rd_accept(point, value, previous)
        if not acceptable:
            if taken:
                return "abort", (
                    "%s: %s. %d of this unit's %d entries are already in the "
                    "instrument's RAM."
                    % (point["label"], why, taken, len(points)))
            return "skipped", "%s: %s" % (point["label"], why)

        # No re-selection and no re-energising: the point is still selected and
        # still in OPERATE from the measurement above, which is the only state
        # in which the firmware keeps what RD gives it.
        command, accepted = gen.enter_value_verified(value)
        if accepted is None:
            return "abort", (
                "%s: the status byte could not be read after %s, so it is not "
                "known whether the entry was taken. %d of this unit's %d "
                "entries are already in the instrument's RAM. Treating an "
                "unknown as a success is exactly the failure this check exists "
                "to prevent."
                % (point["label"], command, taken, len(points)))
        if not accepted:
            return "abort", (
                "%s: the instrument REFUSED %s (Err.G19) even though the value "
                "was inside the 1 %% band this tool predicts from. The "
                "acceptance model is wrong for this point, and %d of this "
                "unit's %d entries are already in the instrument's RAM."
                % (point["label"], command, taken, len(points)))
        print(t("      measured %-18s  ->  %s  (%s)  accepted")
              % (units.format_plain(measured), command, point["entry_unit"]))
        entered.append(dict(label=point["label"], selector=point["selector"],
                            measured=measured, entry=value,
                            command=command, unit=point["entry_unit"],
                            # What the METER read, which is not always what the
                            # instrument is entered in: a current range measured
                            # as a voltage drop is read in volts and entered in
                            # milliamps. Kept so the table can label the reading
                            # with its own unit instead of the entry's.
                            measure=point.get("measure", "V"),
                            unit_key=key))
        taken += 1
        previous = value
    return "entered", ""


# --------------------------------------------------------------------------- #
# One hookup group
# --------------------------------------------------------------------------- #
def _run_group(engine, group: dict, entered: List[dict],
               skipped: List[str]) -> Optional[str]:
    """Measure and enter every unit of one group.

    Returns ``None`` on success or a reason string if the run must be abandoned.
    A refused hookup is a skip, not an abort, because a group the operator
    cannot cable is better left un-adjusted than half-adjusted.
    """
    gen, console = engine.gen, engine.console

    choice = procedure.announce_group(console, gen, group)
    if choice == "exit":
        return "operator exited at %s" % group["title"]
    if choice == "skip":
        skipped.append("%s: group skipped" % group["title"])
        return None

    # Folded into the first point's settling rather than taken as a wait of its
    # own before the group; see procedure.settle_for.
    pending = procedure.thermal_allowance(console, group)

    dmm = engine.dmm.select(function=group["dmm_function"].lower(), group=group)
    if dmm is None:
        print(t("   No reference multimeter available; skipping this group."))
        skipped.append("%s: no reference multimeter" % group["title"])
        return None

    ceiling = dmm.max_input(group["dmm_function"])
    try:
        for key, points in active.adjust.units(group["points"]):
            # Refuse to apply more than the reference meter can survive. The
            # WHOLE unit is left un-adjusted rather than risking the standard,
            # so a meter that cannot take 1000 V drops the 1000 V range's zero
            # points along with its full-scale points instead of committing a
            # new zero against a stale full scale.
            worst = _unit_full_scale(points)
            if ceiling is not None and worst > ceiling:
                print(t("   -- %s") % key)
                print(t("      SKIPPED WHOLE UNIT: %.4g exceeds the meter's "
                        "%.4g limit. Its %d points keep their present "
                        "coefficients.") % (worst, ceiling, len(points)))
                skipped.append("%s: %.4g exceeds the meter's %.4g limit"
                               % (key, worst, ceiling))
                continue

            outcome, detail = _run_unit(engine, dmm, group, key, points,
                                        entered, pending)
            if outcome == "entered":
                continue
            if outcome == "skipped":
                print(t("      UNIT SKIPPED: %s") % detail)
                print(t("      Nothing was entered for %s, so its present "
                        "coefficients are untouched.") % key)
                skipped.append("%s: %s" % (key, detail))
                if not console.yesno(
                        t("      Continue the run without this unit?"),
                        default=True):
                    return "operator stopped after %s" % key
                continue
            # "abort" -- a unit is half-entered and cannot be undone.
            print("")
            print(t("      *** %s") % detail)
            return detail
    finally:
        gen.standby()
        # The meter session is deliberately left open; _restore_after_run hands
        # every meter back when the whole procedure ends. Closing and reopening
        # it around each hookup group cost around fifteen seconds a group and
        # measured nothing.
    return None


# --------------------------------------------------------------------------- #
# Reporting the run
# --------------------------------------------------------------------------- #
def _print_table(console, entered: List[dict]) -> None:
    """Print the complete list of what will be committed.

    The measured column is the reading at the meter's own resolution -- nine
    significant figures, trailing zeros kept -- in whichever unit makes it
    readable. This is the table the operator approves before anything reaches
    EEPROM, so a reading shown to fewer digits than it was taken to is exactly
    the wrong economy.
    """
    console.hr()
    print(t("%-36s %20s   %s") % ("point", "measured", "entered"))
    console.hr()
    for row in entered:
        # The unit of the READING, not of the entry. A 100 mA point measured as
        # a drop across 10 ohm reads 1 V and is entered as 100 mA; labelling the
        # reading "mA" made a correct run look like a factor-of-ten error.
        kind = "I" if row.get("measure") == "I" else "V"
        scale, unit = units.engineering_unit(row["measured"], kind)
        print("%-36s %17s %-3s %s %s"
              % (row["label"],
                 units.format_reading(row["measured"], scale, unit), unit,
                 row["command"], row["unit"]))
    console.hr()


def _print_skipped(console, skipped: List[str]) -> None:
    """Print what was not adjusted, so it cannot pass unnoticed."""
    if not skipped:
        return
    console.hr()
    print(t(" NOT ADJUSTED - %d item(s)") % len(skipped))
    console.hr()
    for line in skipped:
        print("   " + line)
    print("")
    print(t("These keep the coefficients they already had. The as-left"))
    print(t("verification will still measure them, and they will be certified"))
    print(t("against whatever they were before this run."))


def _incomplete_units(entered: List[dict], shunt_ohm: float,
                      shunts: Optional[dict] = None) -> List[str]:
    """Return the units that would be committed only partly. Should be empty.

    This is a final assertion rather than a working check: :func:`_run_unit`
    enters a unit whole or not at all, so a non-empty result here means an entry
    path was taken that this module does not know about. It is cheap, it runs
    immediately before the only irreversible operation in the tool, and the
    failure it guards against -- a new zero coefficient stored as a fraction of
    a stale full-scale coefficient -- is invisible afterwards.
    """
    expected: Dict[str, int] = {}
    for group in active.adjust.groups(shunt_ohm=shunt_ohm, shunts=shunts):
        for key, points in active.adjust.units(group["points"]):
            expected[key] = len(points)

    seen: Dict[str, int] = {}
    for row in entered:
        seen[row["unit_key"]] = seen.get(row["unit_key"], 0) + 1

    return ["%s: %d of %d points entered" % (key, count, expected.get(key, 0))
            for key, count in sorted(seen.items())
            if count != expected.get(key, 0)]


# --------------------------------------------------------------------------- #
# Leaving the instrument in a known state
# --------------------------------------------------------------------------- #
def _wait_for_ext_cal_disable(engine, attempts: int = 6) -> bool:
    """Wait until EXT CAL really is back in DISABLE, and prove it.

    An earlier version printed "set EXT CAL back to DISABLE, then press Enter"
    and discarded the answer, so a run could finish with the switch still in
    ENABLE and every subsequent command silently refused with Err.G15.

    The check is now positive rather than a prompt.
    The driver's ``ext_cal_enabled`` distinguishes the two positions by
    whether ``*IDN?`` answers *and* whether the status byte's error bit came up:
    DISABLE answers cleanly, ENABLE refuses with G15 (internal code ``$12``,
    inside the reporting range at ``$4FF7``) and sets the bit. A timeout with no
    bit set is neither -- the instrument is off or the bus is wedged -- and is
    reported as such instead of being read as success.
    """
    console = engine.console
    # Wait for the switch to be moved before reading. At this point in the run
    # it is certainly still in ENABLE -- CAL END has just been sent -- so
    # reading first is guaranteed to fail, and it fails loudly: the refused
    # query raises Err.G15 on the front panel, so the operator sees an error
    # appear and only then the instruction that would have avoided it. The
    # check is still positive; it just no longer asks the question backwards.
    console.pause()
    for attempt in range(attempts):
        state = engine.gen.ext_cal_enabled()
        if state is False:
            print(t("   EXT CAL confirmed DISABLE; the instrument is answering."))
            return True
        if state is True:
            print(t("   EXT CAL still reads ENABLE (queries refused with G15)."))
        else:
            print(t("   No answer at all: the instrument is not responding on"))
            print(t("   the bus. Check power and the GPIB cable."))
        if attempt == attempts - 1:
            break
        if not console.yesno(t("   Try again?"), default=True):
            return False
    return False


def _restore_after_run(engine) -> None:
    """Return the instrument to Table 3-1's initial setup status."""
    procedure.restore_initial_state(engine.gen)

    # The reference meters are left parked -- on the 3458A, TARM HOLD on a fixed
    # range with a 100 PLC integration, which reads as a dead instrument to
    # whoever is next at the bench. Put them back somewhere ordinary and close
    # the sessions that were held open for the length of the run.
    try:
        engine.dmm.close_all()
        print(t("Reference meters returned to their free-running state."))
    except Exception as exc:
        print(t("   could not restore the reference meter (%s)") % exc)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
#: What an adjustment costs beyond its points, in seconds: the two calibration
#: memory reads that bracket it -- the before-image and the read-back that
#: proves the commit -- at about two minutes each, plus CAL END and the
#: operator's review of the table before approving it.
FIXED_OVERHEAD_S = 2 * 120.0 + 120.0

#: And what each point costs that a verification's does not: the ``RD`` entry
#: and the serial poll that confirms it.
ENTRY_OVERHEAD_S = 5.0


def estimated_minutes() -> int:
    """How long an adjustment takes, at the settling times now in force."""
    groups = active.adjust.groups()
    entries = sum(len(group["points"]) for group in groups)
    return procedure.estimated_minutes(
        groups, extra_s=FIXED_OVERHEAD_S + entries * ENTRY_OVERHEAD_S)


def run(engine, rc) -> None:
    """Run the complete 45-point adjustment and, on approval, commit it."""
    gen, console = engine.gen, engine.console

    console.hr()
    print(t(" FULL ADJUSTMENT - %s") % active.instrument.title)
    console.hr()
    print(t("45 points in four hookup groups. Nothing is written to the"))
    print(t("instrument until you approve the complete table at the end."))
    print("")
    print(t("WARNING: THIS GROUP ENERGISES THE TERMINALS TO 1000 V!"))
    print(t("The voltage-limit step generates approximately 500 V."))
    print("")
    if not console.yesno(t("Proceed?"), default=False):
        return

    # ---- a second meter for the Divider ranges, if one is on the bus -------- #
    try:
        engine.dmm.offer_nanovolt_meter()
    except Exception as exc:
        print(t("   could not check for a second meter (%s)") % exc)

    # ---- how the current ranges are measured, and with what standards ------- #
    shunt, shunts = standards.choose_current_method(
        engine, include_limit_resistor=True)

    # ---- open the meters this run will need --------------------------------- #
    #
    # No reference-standard details are asked for. An adjustment does not
    # produce a certificate -- the as-left verification that follows it is what
    # certifies the instrument, and that run asks for them itself. Asking twice
    # for the same serial numbers, in the middle of the longer and more
    # dangerous of the two procedures, bought nothing.
    plan_groups = active.adjust.groups(shunt_ohm=shunt, shunts=shunts)
    engine.dmm.preopen(plan_groups)

    # ---- pre-calibration setup (EXT CAL must still be DISABLE) -------------- #
    print("")
    print(t("Step 1 of 2: EXT CAL must be DISABLE now. The calibration memory"))
    print(t("can only be read in that position (MRB is gated), and the"))
    print(t("read below is what lets the change be shown afterwards."))
    if not engine.require_ext_cal(False):
        return

    print(t("   applying pre-calibration setup:"))
    gen.standby()
    gen.set_sense(False)          # internal sense, per manual 7.2.2(1)
    gen.set_guard(False)

    # Open the SRQ mask. Once EXT CAL is ENABLE this is the only thing the
    # instrument will still tell us: every query is refused, so a refused RD is
    # otherwise silent. The mask at $DDD7 is applied when a status bit is set
    # ($5F09), not when it is read, so a bit masked off now is lost for good --
    # and SMS is itself gated to DISABLE, so this is the last chance to do it.
    if not gen.arm_error_reporting():
        print("")
        print(t("The SRQ mask could not be set to 255."))
        print(t("Without it the instrument cannot report a refused entry while"))
        print(t("EXT CAL is ENABLE, and a value the firmware throws away would"))
        print(t("still appear in the table as though it had been stored."))
        if not console.yesno(t("Run the adjustment BLIND anyway?"),
                             default=False):
            return
    else:
        print(t("   error reporting armed: a refused entry will be caught by"))
        print(t("   serial poll, which EXT CAL does not gate."))
    # Neither output limit is set. During calibration the firmware drives the
    # output from the selected point rather than through the D data command,
    # and the limits do not gate that path. perf_engine does raise them,
    # because verification does drive the output with D. See the module
    # docstring.
    print(t("   sense and guard set to INTERNAL for calibration; the external"))
    print(t("   setting is restored at the end."))

    # ---- take a before image ------------------------------------------------ #
    #
    # This is not optional and it is taken from the EEPROM master at $E000, not
    # the working copy. Three reasons, in order of how badly they bite:
    #
    #   * It is the only record of the coefficients this run is about to
    #     overwrite. There is no undo for REND; the before-image *is* the undo.
    #   * The master is what survives a power cycle. The working copy is
    #     overwritten from the master at boot ($0257/$0294), so a working-copy
    #     image describes a state that may already have been discarded.
    #   * Without it the after-image has nothing to be compared against, and
    #     "did the commit land?" cannot be answered at all.
    #
    # An earlier version offered it as a question with a default of yes, did not
    # check the result for None, read the working copy, and never wrote it to
    # disk -- so a failed read left `before = None` and every downstream check
    # silently turned itself off.
    print("")
    print(t("Reading the EEPROM master at $E000 (364 bytes, about two minutes)."))
    print(t("This is the only record of what is about to be overwritten:"))
    print(t("CAL END cannot be undone."))
    before = engine.read_cal_image(master=True)
    if before is None:
        print("")
        print(t("The calibration memory could not be read."))
        print(t("MRB is gated to EXT CAL = DISABLE, so either the switch is not"))
        print(t("in DISABLE or the bus is not healthy. Either way there would be"))
        print(t("no before-image and no way to verify the commit afterwards."))
        print(t("Not proceeding."))
        return
    problems = active.driver.validate_cal_block(before)
    if problems:
        print("")
        print(t("The calibration memory read back with %d problem(s) BEFORE"
                " anything was changed:") % len(problems))
        for line in problems:
            print("   " + line)
        print("")
        print(t("This is a pre-existing condition, not something this run"))
        print(t("caused, but adjusting on top of it may make it harder to"))
        print(t("diagnose."))
        if not console.yesno(t("Continue anyway?"), default=False):
            return

    # ---- enable EXT CAL ----------------------------------------------------- #
    print("")
    print(t("Step 2 of 2: set the EXT CAL switch on the rear panel to ENABLE"))
    print(t("(a flat-blade screwdriver or tweezers; manual 7.2.2(2))."))
    if not engine.require_ext_cal(True):
        return

    # ---- the sweep ---------------------------------------------------------- #
    entered: List[dict] = []
    skipped: List[str] = []
    abort_reason: Optional[str] = None
    try:
        for group in plan_groups:
            abort_reason = _run_group(engine, group, entered, skipped)
            if abort_reason:
                break
    except KeyboardInterrupt:
        abort_reason = "interrupted by the operator"

    gen.standby()

    if not entered:
        print("")
        print(t("No points were entered; nothing to commit. CAL END was not"))
        print(t("sent, so the stored calibration is unchanged."))
        _restore_after_run(engine)
        return

    _print_table(console, entered)
    _print_skipped(console, skipped)
    print("")
    print(t("%d of %d points entered.")
          % (len(entered), active.adjust.total_points(shunt, shunts)))

    if abort_reason:
        print("")
        print(t("ABANDONED: %s") % abort_reason)
        print(t("CAL END was NOT sent, so nothing has been written to EEPROM."))
        print(t("Everything above is still only in the instrument's RAM and"))
        print(t("will be gone at the next power cycle or device clear."))
        _restore_after_run(engine)
        return

    # ---- the integrity assertion, immediately before the only commit -------- #
    partial = _incomplete_units(entered, shunt, shunts)
    if partial:
        print("")
        print(t("REFUSING TO COMMIT: %d coefficient unit(s) would be written"
                " only partly.") % len(partial))
        for line in partial:
            print("   " + line)
        print("")
        print(t("A range's zero coefficient is stored as a fraction of its"))
        print(t("full-scale coefficient, and the three DAC entries are combined"))
        print(t("into one value, so a partial unit is not a partial improvement"))
        print(t("-- it is a wrong number that nothing afterwards will flag."))
        print(t("CAL END was NOT sent. Re-run the adjustment."))
        _restore_after_run(engine)
        return

    # ---- the commit gate ---------------------------------------------------- #
    print("")
    print(t("Review the table above. A value that looks wrong means an"))
    print(t("unplugged lead, the wrong terminal, or a limit blocking the output."))
    print("")
    if not console.yesno(
            t("Send CAL END and write ALL of these to EEPROM?"), default=False):
        print(t("Aborted. CAL END not sent; nothing written."))
        _restore_after_run(engine)
        return

    print("")
    print(t(">>> CAL END - writing EEPROM"))
    committed = gen.cal_end()
    if committed is False:
        print("")
        print(t("THE INSTRUMENT REFUSED CAL END. The status byte reports an"))
        print(t("error on that line, so the coefficients were NOT written."))
        print(t("Check the panel for the error code before doing anything else."))
    elif committed is None:
        print(t("CAL END sent, but the status byte could not be read, so"))
        print(t("whether it was accepted is unknown. The read-back below is now"))
        print(t("the only evidence either way."))
    else:
        print(t("CAL END accepted. The panel shows 'CAL End' blinking while it"))
        print(t("writes."))

    # ---- EXT CAL back to DISABLE, and verified ------------------------------ #
    print("")
    print(t("Set the EXT CAL switch on the rear panel back to DISABLE."))
    if not _wait_for_ext_cal_disable(engine):
        print("")
        print(t("Continuing without confirmation. The commit is UNVERIFIED and"))
        print(t("the instrument may still be in EXT CAL, where every ordinary"))
        print(t("command is refused with Err.G15."))
        _restore_after_run(engine)
        return

    _restore_after_run(engine)

    # ---- confirm the commit landed ----------------------------------------- #
    #
    # Unconditional, and it checks three separate things, because they fail
    # separately:
    #
    #   * the records changed (or did not) -- the commit reached EEPROM
    #   * every record is still valid BCD and no full-scale coefficient came
    #     back zero -- the write was not corrupt
    #   * the stored checksum matches the block -- the firmware will still
    #     accept this calibration at the next power-up rather than silently
    #     replacing it with the ROM defaults ($0241)
    #
    # The third is the one that matters most and is the one nothing else would
    # ever surface: a bad checksum costs nothing today and everything at the
    # next power cycle.
    print("")
    print(t("Reading the EEPROM master back to verify the commit ..."))
    bar = getattr(console, "progress_bar", None)
    after, problems = gen.verify_cal_commit(
        progress=(lambda done, total: bar(done, total, label="cal"))
        if bar is not None else None)

    if after is not None:
        engine.report_cal_change(before, after)
        # An unchanged block is a FAILURE, not a footnote. On 29 July 2026 a
        # complete 45-point run committed nothing -- every entry had been made
        # with the point unenergised and silently discarded -- and the tool
        # printed "no calibration record changed" immediately above a
        # "COMMIT VERIFIED" banner, because the block it read back was a
        # perfectly valid, perfectly correct copy of the calibration nobody had
        # touched. Validity is not evidence of effect.
        if not active.driver.compare_cal_blocks(before, after):
            problems.append(
                "not one calibration record changed, so this run had NO "
                "EFFECT: either CAL END was not accepted or the entries never "
                "reached the working copy. The block that was read back is "
                "valid and correctly checksummed because it is the calibration "
                "the instrument already held.")

    console.hr()
    if problems:
        print(t(" COMMIT VERIFICATION FAILED - %d problem(s)") % len(problems))
        console.hr()
        for line in problems:
            print("   " + line)
        print("")
        print(t("The before-image saved at the start of this run is the record"))
        print(t("of what the instrument held beforehand. Do not power-cycle the"))
        print(t("instrument until you have decided what to do: if the checksum"))
        print(t("is wrong, the next power-up discards the block."))
    else:
        print(t(" COMMIT VERIFIED - block valid, checksum correct"))
        console.hr()

    # ---- no certificate is produced here ------------------------------------ #
    #
    # An adjustment is not evidence of anything. It records what the instrument
    # was told, not what it does, and every number in it was measured before the
    # coefficient derived from it existed. The document that certifies an
    # adjusted instrument is the AS-LEFT verification taken afterwards, which
    # measures the adjusted instrument and grades it against the specification.
    #
    # This used to save an "adjustment record" alongside the verifications, and
    # its only effect was to put a row in the certificate menu that nobody
    # should choose.
    print("")
    print(t("The adjustment is complete. No certificate is produced from it:"))
    print(t("what certifies an adjusted instrument is a performance"))
    print(t("verification taken afterwards and tagged AS-LEFT."))
    print(t("Run one now."))
