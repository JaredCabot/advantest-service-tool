"""Automated performance verification, for whichever instrument is bound.

Drives the whole verification with minimum interaction: the operator makes the
connection a group asks for, and the engine sets the generator up, takes the
readings on the reference multimeter, grades them against the published accuracy
specification the instrument plugin supplies, and saves the run for the
certificate
generator.

The run is tagged AS-FOUND or AS-LEFT by the operator, so a verification taken
before an adjustment and one taken after can be combined into a single
certificate showing both.

Every group can be run, skipped or exited (``[R]``/``[S]``/``[X]``).

This module is read-only with respect to the instrument's calibration: it
selects ranges, sets outputs and reads the DMM. No calibration command is sent
and the EXT CAL switch must remain in DISABLE throughout -- with it in ENABLE
the range and output commands would all be refused with Err.G15.

``engine`` is the :class:`CalibrationEngine` from the service tool, which
supplies ``gen`` (the instrument driver), ``console``, ``certs`` and ``dmm``
(the DMM manager).

Nothing here names an instrument. Every point, tolerance and hookup comes from
the plugin bound in :mod:`active`, so this engine verifies whatever is plugged
in.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

import active
import cal_report
import procedure
import standards
import uncertainty
import units

# The instrument's own modules -- driver, specification, verification plan --
# are reached through :mod:`active` rather than imported, because which ones
# they are is not known until the operator has chosen an instrument. Timing
# settings are read as ``active.driver.SETTLE_S`` at the moment they are used,
# for the same reason a from-import was wrong before: it would bind whatever
# the value was at import time and ignore the operator's setting.
from i18n import t


# --------------------------------------------------------------------------- #
# DMM range selection
#
# The verification spans 10 microvolts to 1000 volts, so a fixed DMM range is
# not workable. A full-scale point is measured on the smallest range that
# contains it; a zero point is autoranged, because after a failed adjustment the
# offset could be anything and a fixed low range would simply overload.
# --------------------------------------------------------------------------- #
_DCV_RANGES = (0.1, 1.0, 10.0, 100.0, 1000.0)
_DCI_RANGES = (1e-3, 1e-2, 1e-1)


def _dmm_range(point: dict) -> str:
    """Return the DMM range string for one verification point.

    Chosen from what the meter will see rather than from what the generator was
    asked for: a shunt point commands milliamps and measures volts, and ranging
    a voltmeter to 1 mA would ask for a range that does not exist.
    """
    magnitude = abs(active.perf.measured_si(point))
    if magnitude == 0.0:
        return "AUTO"
    voltage = point["kind"] != "I" or point.get("shunt_ohm")
    table = _DCV_RANGES if voltage else _DCI_RANGES
    for candidate in table:
        if magnitude <= candidate * 1.05:
            return ("%g" % candidate)
    return "AUTO"


def _nplc(point: dict) -> None:
    """Return the integration time to request for one point.

    ``None``, deliberately: the integration time belongs to the meter, not to
    the point, so it comes from that meter's own file in ``dmm_configs/`` --
    the ``nplc`` key, which is 100 for the 3458A. Passing ``None`` to
    :meth:`dmm_driver.DMM.measure` is what selects it.
    """
    return None


# --------------------------------------------------------------------------- #
# Result rows
# --------------------------------------------------------------------------- #
def _budget_function(point: dict) -> str:
    """Which section of the uncertainty budget applies to this point.

    Four measurements, not two: the divider ranges may be read by a different
    meter from the main output, and a current range measured as a voltage drop is
    a voltage measurement plus a resistor rather than a current measurement.
    """
    if point.get("shunt_ohm"):
        return "shunt"
    if point["kind"] == "I":
        return "dci"
    return ("divider" if active.driver.RANGES[point["range"]].get("divider")
            else "dcv")


def _row(point: dict, measured_si: Optional[float], interval: str,
         section: str, group: str, status: Optional[str] = None,
         spread_si: Optional[float] = None,
         budget: Optional[uncertainty.Budget] = None) -> dict:
    """Build one graded results row, scaled to a friendly engineering unit.

    ``section`` becomes a heading on the certificate; ``group`` draws a rule
    wherever it changes, so each range reads as its own block. A row with
    ``measured_si`` of ``None`` is recorded but not graded, which is how a
    skipped or failed reading is represented without affecting the verdict.

    The deviation and the verdict are **not** computed here. They come from
    ``cal_report.make_row``, which derives both from the numbers, so a row can
    never carry a verdict its own figures contradict. All this does is choose
    the display unit and scale every field into it -- the certificate prints
    them side by side, so a row that mixed volts and millivolts would be
    unreadable and, worse, would look reasonable.

    ``budget`` is the laboratory's evaluated uncertainty budget, if it has
    written one. It is applied at this point's own reading and range, and
    ``spread_si`` -- the agreement between the two readings just taken -- is
    offered to it as a type-A term.
    """
    setting = point["setting_si"]
    limit = active.spec.tolerance(point["range"], setting, interval)

    scaled_fs, unit = active.perf.display_value(point["fs_si"], point["kind"])
    factor = (scaled_fs / point["fs_si"]) if point["fs_si"] else 1.0

    def scale(value):
        return None if value is None else value * factor

    expanded = None
    if budget is not None and not budget.is_empty():
        expanded = budget.expanded_for(
            _budget_function(point), point["range"], measured_si,
            point["fs_si"], spread_si=spread_si,
            shunt_ohm=point.get("shunt_ohm"))

    row = cal_report.make_row(
        "%s %s" % (point["range"], point["point"]),
        scale(setting),                     # nominal
        scale(measured_si),                 # measured
        scale(limit),                       # tolerance
        scale(expanded),                    # expanded uncertainty
        unit=unit,
        is_dc=True,                         # every point on this instrument is
        standard=scale(setting),            # the setting applied
        group=group,
        section=section,
        status=status)
    if budget is not None and budget.guard_band():
        cal_report.apply_guard_band(row)
    return row


def _print_row(row: dict) -> None:
    """Echo a graded row to the console, at the meter's own resolution.

    Three decisions, all in aid of a line an operator can read at a glance:

    * **One unit for the whole line.** Chosen from the larger of the reading and
      its tolerance, so a zero point is shown in the unit of the limit it is
      being judged against rather than three prefixes below it. A line that
      mixed microvolts and volts would need reading twice.
    * **Nine significant figures on the reading**, trailing zeros included. That
      is what an 8.5-digit meter returns, and a reading printed to fewer digits
      than it was measured to is a reading thrown away.
    * **Plain decimal, never scientific.** A divider zero is microvolts, and
      ``1.24570525e-05`` cannot be compared against a tolerance by eye.
    """
    verdict = ("PASS" if row["passed"] else
               ("FAIL" if row["passed"] is False else "-"))
    if row["measured"] is None:
        print(t("   %-14s  not measured") % row["parameter"])
        return

    # The row is already scaled into a unit chosen from the range's full scale,
    # and for every point that generates something that is the right unit --
    # keeping it means the whole range reads in one unit instead of slipping
    # between millivolts and volts between +50 % and +F.S.
    #
    # A zero point is the exception: its full scale says nothing about a reading
    # that is six decades below it. There, the tolerance is the yardstick, so the
    # unit is chosen from that.
    unit = row.get("unit", "")
    per_unit = units.SI_PER_UNIT.get(unit, 1.0)
    kind = "I" if unit.endswith("A") else "V"
    measured = row["measured"] * per_unit
    deviation = (row["deviation"] or 0.0) * per_unit
    tolerance = (row["tolerance"] or 0.0) * per_unit

    scale = per_unit
    if not row.get("nominal"):
        scale, unit = units.engineering_unit(tolerance or measured, kind)
    print(t("   %-14s meas %14s %-3s dev %13s  tol %12s  -> %s")
          % (row["parameter"],
             units.format_reading(measured, scale, unit), unit,
             units.format_plain(deviation / scale, 4),
             units.format_plain(tolerance / scale, 4), verdict))


def _apply_limits(gen, volts: int, milliamps: int) -> None:
    """Set the output limits to what the coming work needs.

    A non-Divider range is selected first because ``VL`` and ``IL`` are refused
    outright in Divider mode (Err.G16), and the instrument may well be sitting
    on a Divider range from the previous group. The 10 V range is an arbitrary
    safe choice: it is not a Divider range and cannot energise the terminals
    dangerously while the limits are being changed.
    """
    gen.standby()
    gen.select_range("10V")
    gen.set_voltage_limit(volts)
    gen.set_current_limit(milliamps)


def _restore_limits(gen) -> None:
    """Return the output limits to the instrument's own initial values.

    Called however a run ends, including an abort, so an elevated limit is
    never left armed for whoever uses the instrument next. Failures are
    swallowed: this runs on the way out and must not mask the real reason a run
    ended.
    """
    try:
        _apply_limits(gen, active.perf.SAFE_VOLTAGE_LIMIT,
                      active.perf.SAFE_CURRENT_LIMIT)
        gen.standby()
    except Exception:
        pass


def estimated_minutes(short: bool = False) -> int:
    """How long a verification takes, at the settling times now in force.

    Quoted on the menu. The direct-current plan is used because it is the
    default and the shorter of the two; choosing the voltage-drop method adds
    two more hookups.
    """
    return procedure.estimated_minutes(active.perf.plan(short=short))


def _current_method_note(shunts: Optional[dict]) -> str:
    """One sentence for the certificate on how the current ranges were measured.

    Written from what was actually done rather than from which menu item was
    chosen, because the two methods can be mixed range by range: a bench with
    one standard resistor uses it where it helps and the meter's own current
    terminals elsewhere.
    """
    if not shunts:
        return (" The current ranges were measured directly, through the "
                "reference multimeter's own current terminals.")

    def joined(names: List[str]) -> str:
        if len(names) < 2:
            return "".join(names)
        return "%s and %s" % (", ".join(names[:-1]), names[-1])

    used = [name for name in standards.CURRENT_RANGES if name in shunts]
    direct = [name for name in standards.CURRENT_RANGES if name not in shunts]
    note = (" The %s current range%s %s measured by the Operation Manual's "
            "voltage-drop method, using %s of "
            % (joined(used), "" if len(used) == 1 else "s",
               "was" if len(used) == 1 else "were",
               "a standard" if len(used) == 1 else "standards")
            + joined(["%.9g ohm on %s" % (shunts[name], name)
                      for name in used]) + ".")
    if direct:
        note += (" The %s range%s %s measured directly, through the reference "
                 "multimeter's own current terminals."
                 % (joined(direct), "" if len(direct) == 1 else "s",
                    "was" if len(direct) == 1 else "were"))
    return note


def _ask_aspect(console) -> str:
    """Ask whether this run records the state before or after adjustment."""
    answer = console.ask(
        t("Is this an [F] as-found or [L] as-left verification? [F]: "),
        default="F").strip().lower()
    return "asleft" if answer.startswith("l") else "asfound"


def _identify_uut(console, gen, rc) -> None:
    """Settle who the certificate is about, before it starts measuring them.

    Four of the five item-under-test lines come from ``*IDN?`` and are shown
    rather than asked -- a certificate that describes the instrument from
    something typed can describe an instrument that was never on the bench. The
    serial number and the option list are not in ``*IDN?``, so they are asked
    here, at the start of the run, along with everything else the operator is
    asked while the instrument and its paperwork are still in front of them.

    Answers are kept on the generator for the rest of the session, so a
    verify - adjust - verify sequence types them once.
    """
    print("")
    console.hr()
    print(t(" INSTRUMENT UNDER TEST"))
    console.hr()
    if not gen.idn:
        # Only reachable with EXT CAL in ENABLE, which gates *IDN?. The run
        # cannot get this far in that state, so this is defensive.
        print(t("The instrument has not answered *IDN?, so the certificate"))
        print(t("will carry this tool's defaults for the first three lines."))
    print("   %-14s %s" % (t("Description"), gen.description))
    print("   %-14s %s" % (t("Model"), gen.model))
    print("   %-14s %s" % (t("Firmware"), gen.firmware or "-"))
    print("")
    print(t("The %s reports no serial number and no option list, so those")
          % active.instrument.name)
    print(t("two are typed. The serial number is on the rear-panel label."))
    print(t("Press Enter to accept the value in brackets."))
    preset = getattr(rc, "uut", None) or {}
    gen.serial = standards.ask_with_default(
        console, t("   Serial number"), gen.serial or preset.get("serial", ""))
    gen.options = standards.ask_with_default(
        console, t("   Options"), gen.options or preset.get("options", ""))


# --------------------------------------------------------------------------- #
# One hookup group
# --------------------------------------------------------------------------- #
def _verify_group(engine, group: dict, results: List[dict],
                  interval: str,
                  budget: Optional[uncertainty.Budget] = None) -> bool:
    """Measure every point of one hookup group. Returns ``False`` to abort."""
    gen, console = engine.gen, engine.console

    # Folded into the first point's settling rather than taken as a wait of its
    # own before the group; see procedure.settle_for.
    pending = procedure.thermal_allowance(console, group)

    dmm = engine.dmm.select(function=group["dmm_function"].lower(), group=group)
    if dmm is None:
        print(t("No reference multimeter available; skipping this group."))
        return True

    # Raise the output limits only as far as this group needs, and only for as
    # long as it runs; _restore_limits puts them back when the run ends.
    _apply_limits(gen, group.get("voltage_limit", active.perf.SAFE_VOLTAGE_LIMIT),
                  group.get("current_limit", active.perf.SAFE_CURRENT_LIMIT))

    ceiling = dmm.max_input(group["dmm_function"])
    try:
        for point in group["points"]:
            section = "range:%s" % point["range"]
            grp = "range:%s" % point["range"]
            spec = active.driver.RANGES[point["range"]]

            # Refuse to apply more than the reference meter can survive. The
            # 34420A is an example: excellent on the Divider ranges, but its
            # highest range is 100 V and a 1000 V range would reach
            # its terminals if nothing stopped it. Compared against what the
            # meter sees, so a shunt point is judged by its voltage drop.
            arriving = abs(active.perf.measured_si(point))
            if ceiling is not None and arriving > ceiling:
                print(t("   %-14s SKIPPED: %.4g exceeds the meter's %.4g limit")
                      % (point["range"] + " " + point["point"],
                         arriving, ceiling))
                results.append(_row(point, None, interval, section, grp,
                                    status="overrange", budget=budget))
                continue

            gen.select_range(point["range"])
            # The driver decides the unit from the range. D takes volts on the
            # main ranges and MILLIVOLTS on the Divider ranges, so sending an
            # SI value on those put out a thousandth of what was asked for --
            # every Divider full-scale point measured as very nearly zero.
            gen.set_output_on_range(point["range"], point["setting_si"])

            # Range the meter before energising, not after: the signal should
            # never arrive at a meter still set up for the previous point, and
            # a range change inside the settling window would corrupt the
            # reading that window exists to protect.
            try:
                dmm.setup(group["dmm_function"], _dmm_range(point), _nplc(point))
            except Exception as exc:
                print(t("   %s: DMM setup failed (%s)")
                      % (point["range"] + " " + point["point"], exc))
                results.append(_row(point, None, interval, section, grp,
                                    status="noread", budget=budget))
                continue

            gen.operate()
            seconds, label = procedure.settle_for(point, pending)
            units.wait_with_progress(seconds, label=label)

            try:
                measured, spread = dmm.measure(
                    group["dmm_function"], _dmm_range(point), _nplc(point),
                    samples=2, configure=False)
            except Exception as exc:
                print(t("   %s: DMM read failed (%s)")
                      % (point["range"] + " " + point["point"], exc))
                results.append(_row(point, None, interval, section, grp,
                                    status="noread", budget=budget))
                continue
            finally:
                gen.standby()

            # A shunt point was read in volts; the specification, the tolerance
            # and every other point in the run are in amps. Convert here, once,
            # so that nothing downstream has to know which method was used.
            shunt_ohm = point.get("shunt_ohm")
            if shunt_ohm:
                measured = measured / shunt_ohm

            # The spread between the two readings just taken is offered to the
            # budget as its type-A term, converted to the point's own domain the
            # same way the reading was.
            row = _row(point, measured, interval, section, grp,
                       spread_si=(spread / shunt_ohm if (shunt_ohm and spread)
                                  else spread),
                       budget=budget)
            results.append(row)
            _print_row(row)
    finally:
        gen.standby()
        # The meter is deliberately NOT restored or closed here. It was, once
        # per group, and the cost was around fifteen seconds of every hookup
        # change: PRESET NORM leaves the 3458A free-running, so the drain that
        # follows it reads its full forty-reading limit rather than timing out,
        # and the reconnect then drains twenty more. The meter is handed back
        # once, when the run ends, by DmmManager.close_all().
    return True


# --------------------------------------------------------------------------- #
# Rerunning a group that failed
# --------------------------------------------------------------------------- #
def _offer_rerun(console, group: dict, rows: List[dict]) -> bool:
    """Report a group's failures and ask whether to measure it again.

    A failing point is much more often a hookup than an instrument: a lead on
    the wrong terminal, a meter still on its front inputs, a clip that has come
    off the divider terminals. Making the operator restart the whole
    verification to correct that is what this avoids.

    The warning is not decoration. Repeating a measurement until it passes is
    not a verification, and the operator is the only one who can tell the
    difference between fixing a mistake and fishing for an answer.
    """
    failed = [row for row in rows if row["passed"] is False]
    if not failed:
        return False
    print("")
    console.hr()
    print(t(" %d of %d points FAILED in %s")
          % (len(failed), len([r for r in rows if r["passed"] is not None]),
             group["title"]))
    console.hr()
    for row in failed[:8]:
        _print_row(row)
    if len(failed) > 8:
        print(t("   ... and %d more.") % (len(failed) - 8))
    print("")
    print(t("Rerun this group only if something about the setup was wrong --"))
    print(t("Such as a disconnected or incorrectly terminated lead etc."))
    print(t("The readings above are then DISCARDED and replaced."))
    print(t("Measuring again in the hope of a better result is not a viable"))
    print(t("verification. Continued failed tests indicates a fault."))
    return console.yesno(t("Rerun %s?") % group["title"], default=False)


# --------------------------------------------------------------------------- #
# Save
# --------------------------------------------------------------------------- #
def _section_labels(rows: List[dict]) -> Dict[str, str]:
    """Printed heading for each section tag the rows carry.

    The certificate draws a shaded band wherever the section changes and takes
    the wording from this map. Rows are tagged ``range:<name>``.

    The headings are the instrument's own range designations and carry no
    English prose, because one saved run can be rendered as a certificate in any
    of the installed languages and these labels are fixed when the run is saved,
    not when it is printed. A heading reading "10 mV range" would stay in
    English on the Japanese certificate; "10 mV DIV" is what the front panel
    says in every language.
    """
    labels: Dict[str, str] = {}
    for row in rows:
        key = row.get("section")
        if not key or key in labels:
            continue
        name = key.split(":", 1)[-1]
        spec = active.driver.RANGES.get(name)
        suffix = " DIV" if (spec and spec.get("divider")) else ""
        labels[key] = "%s%s" % (name, suffix)
    return labels


def _save(engine, rc, aspect: str, results: List[dict],
          standard_rows: List[dict], interval: str,
          shunts: Optional[dict] = None,
          budget: Optional[uncertainty.Budget] = None) -> None:
    """Assemble and persist the run for later certificate generation."""
    # ``_current_method_note`` is what the certificate says about how the
    # current ranges were measured, which is not one sentence any more: the
    # methods can be mixed, and a certificate that claimed the drop method for
    # all three when one was read directly would be a false statement about
    # traceability.
    key = "type_asleft" if aspect == "asleft" else "type_asfound"
    method = (
        "%s performance verification. Each range is generated at "
        % active.instrument.title +
        "the points listed and measured with the reference multimeter shown; "
        "the deviation is graded against the manufacturer's published total "
        "accuracy specification (Operation Manual section 8.1, table 2), %s "
        "interval." % active.spec.INTERVAL_NAMES[interval]
        + _current_method_note(shunts)
        + " " + active.spec.conditions_note().replace("\n", " "))
    data, cert_no = engine.certs.build(
        rc, key, results, method, standards.dedup(standard_rows),
        section_labels=_section_labels(results),
        spec_note=active.spec.specification_note(interval))
    data["aspect"] = aspect
    data["interval"] = interval
    data["overall_pass"] = cal_report.overall_verdict(results)
    if budget is not None:
        # The uncertainty paragraph comes from the budget that was actually
        # applied, so the statement and the arithmetic cannot drift apart: a
        # certificate claiming simple acceptance over guard-banded rows, or
        # claiming k = 2 over rows evaluated at some other k, would be worse
        # than the one that claims nothing.
        data.setdefault("strings", {})["unc_body"] = budget.note()
    engine.certs.save(data, cert_no, "verify")

    graded = [r for r in results if r["passed"] is not None]
    failed = [r for r in graded if not r["passed"]]
    label = "AS-LEFT" if aspect == "asleft" else "AS-FOUND"
    print("")
    print(t("Saved %d measured points as an %s verification.")
          % (len(results), label))
    print(t("  %d graded, %d passed, %d failed.")
          % (len(graded), len(graded) - len(failed), len(failed)))
    print(t("Use 'Generate calibration report' to produce the certificate "
            "(a single as-found or as-left, or a combined report once both "
            "exist)."))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run(engine, rc, short: bool = False,
        interval: Optional[str] = None) -> None:
    """Run the performance verification and save the result.

    ``short`` measures only full scale and zero at each polarity; the full plan
    adds the two half-scale points, which are not needed to demonstrate
    conformance but reveal linearity problems the endpoints cannot.

    ``interval`` defaults to the instrument's own default interval, resolved
    here rather than in the signature: a default argument is evaluated when the
    module is imported, which is before any instrument has been chosen.
    """
    if interval is None:
        interval = active.spec.DEFAULT_INTERVAL
    gen, console = engine.gen, engine.console
    console.hr()
    print(t(" PERFORMANCE VERIFICATION - %s") % active.instrument.title)
    console.hr()
    print(t("Specification: total accuracy, %s interval (Operation Manual 8.1).")
          % active.spec.INTERVAL_NAMES[interval])
    print(t(active.spec.conditions_note()))
    print("")

    if not engine.require_ext_cal(False):
        return

    aspect = _ask_aspect(console)
    print(t("Recording an %s verification.")
          % ("AS-LEFT" if aspect == "asleft" else "AS-FOUND"))

    # Measure the way the specification assumes and the manual calibrates:
    # internal sense, straight across the output terminals. Output limits are
    # NOT raised here - each group raises only what it needs, and the finally
    # block below puts them back.
    gen.standby()
    gen.set_sense(False)
    gen.set_guard(False)

    # A second meter for the Divider ranges, if one is on the bus. Asked before
    # the first hookup so the operator knows what they are cabling up.
    try:
        engine.dmm.offer_nanovolt_meter()
    except Exception as exc:
        print(t("   could not check for a second meter (%s)") % exc)

    # How the current ranges are measured. An adjustment asks the same question
    # from the same code; a verification has no current-limit step, so it does
    # not ask about that step.
    _, shunts = standards.choose_current_method(engine,
                                                include_limit_resistor=False)

    results: List[dict] = []
    plan = active.perf.plan(short=short, shunts=shunts)

    # Open every meter the plan will need, now rather than at each hookup. The
    # sessions stay open until the run ends.
    engine.dmm.preopen(plan)

    # The certificate's own two sections, in the order they appear on it, and
    # both before the first hookup: who the certificate is about, then what it
    # claims traceability to. They come after the questions above because those
    # questions decide which standards there are to ask about -- the second
    # meter and the resistors are what the run is traceable to.
    _identify_uut(console, gen, rc)
    standard_rows = standards.dedup(engine.dmm.standard_rows()
                                    + standards.shunt_rows(shunts))
    standards.collect(console, standard_rows)

    # The laboratory's own uncertainty budget, if it has written one. Loaded here
    # rather than at import so that an edit to uncertainty.ini applies to the
    # next run without restarting, and announced because a budget silently not
    # being applied -- a mistyped section name, a file saved elsewhere -- would
    # otherwise show up only as a column of dashes on the finished certificate.
    budget = uncertainty.Budget.load()
    print("")
    if budget.is_empty():
        print(t("No uncertainty budget in uncertainty.ini; the certificate will"))
        print(t("state that none is reported."))
    else:
        print(t("Uncertainty budget loaded: k = %g, %s acceptance.")
              % (budget.k, "guarded" if budget.guard_band() else "simple"))

    print(t("\n%d points across %d hookup groups.")
          % (sum(len(g["points"]) for g in plan), len(plan)))
    print(t("At each group choose: [R]un it, [S]kip it, or e[X]it to the menu."))

    stop = False
    try:
        for group in plan:
            while not stop:
                choice = procedure.announce_group(console, gen, group)
                if choice == "exit":
                    print(t("Exiting; saving whatever has been measured "
                            "so far."))
                    stop = True
                    break
                if choice == "skip":
                    break
                mark = len(results)
                if not _verify_group(engine, group, results, interval,
                                     budget=budget):
                    stop = True
                    break
                if not _offer_rerun(console, group, results[mark:]):
                    break
                # The hookup is shown again with the rerun, because what is
                # being corrected is nearly always the hookup.
                del results[mark:]
                print(t("   Discarded; %s will be measured again.")
                      % group["title"])
            if stop:
                break
    finally:
        # However this ends - normally, by exit, or by an exception - leave the
        # instrument in Table 3-1's initial setup status: limits back to their
        # initial values, output in standby, sense and guard OFF.
        _restore_limits(gen)
        # And hand every meter back free-running rather than parked on a fixed
        # range in TARM HOLD, which looks like a dead instrument at the bench.
        # Once, here, instead of once per hookup group.
        engine.dmm.close_all()
        print("")
        print(t("Output limits returned to VL%d / IL%d.")
              % (active.perf.SAFE_VOLTAGE_LIMIT, active.perf.SAFE_CURRENT_LIMIT))
        procedure.restore_initial_state(gen)

    if not results:
        print(t("No measurements were taken; nothing saved."))
        return
    _save(engine, rc, aspect, results, standard_rows, interval, shunts,
          budget=budget)
