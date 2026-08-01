"""Reading display: units, scaling and the progress bar.

None of this is instrument-specific. It was written for the R6161 and lived in
its driver, where a second instrument would have had to import from the first
one's module or copy it -- so it moved here when the tool became modular.

What it is for
--------------
A verification spans ten microvolts to a thousand volts, and the operator reads
every line of it at the bench. Three decisions follow from that:

* **Plain decimal, never scientific.** A divider zero is microvolts, and
  ``1.24570525e-05`` cannot be compared against a tolerance by eye.
* **Nine significant figures**, trailing zeros kept. That is what an 8.5-digit
  meter returns, and a reading printed to fewer digits than it was measured to
  is resolution paid for in integration time and thrown away.
* **One unit per line**, chosen from the range rather than the reading, so a
  range does not slip between millivolts and volts between its 50 % point and
  its full scale.

The progress bar is here for the same reason: a run spends more of its time
waiting than doing anything else, and a silent minute is indistinguishable from
a hung instrument.
"""

from __future__ import annotations

import math
import sys
import time
from typing import Dict


def format_plain(value, significant=9):
    """Format a number without scientific notation, at ``significant`` digits.

    ``%g`` switches to an exponent below 1e-4, so a divider zero reading prints
    as ``1.24570525e-05``. That is correct and unreadable: an operator comparing
    it against a limit expressed in microvolts has to decode the exponent first,
    and a run sheet full of them invites transcription errors. Everything here
    is between microvolts and kilovolts, a range that fits comfortably in plain
    decimal.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == 0.0 or not math.isfinite(number):
        return "0" if number == 0.0 else str(number)
    exponent = math.floor(math.log10(abs(number)))
    decimals = min(max(significant - 1 - int(exponent), 0), 15)
    text = "%.*f" % (decimals, number)
    # Trim trailing zeros, but leave at least one digit after the point so the
    # column still reads as a measurement rather than a count.
    if "." in text:
        text = text.rstrip("0")
        if text.endswith("."):
            text += "0"
    return text


#: Where EXT-SENSE and EXT-GUARD are left when a procedure finishes.
#:
#: OFF, both of them, which is Table 3-1's initial setup status (Operation
#: Manual 3.1.3, page 3-3). That table is also the reason this is not a matter
#: of taste: EXT-GUARD and EXT-SENSE are among the parameters marked as backed
#: up, so they survive a power cycle. A run that switched them to EXTERNAL for
#: its own purposes and left them there would hand the next person an
#: instrument that reads its own output through their leads, and nothing on the
#: front panel would explain why.
#:
#: An earlier version restored them to EXTERNAL on the grounds that it was
#: "the operator's preference". It was not; it was this tool's own guess, made
#: before Table 3-1 had been read.
INITIAL_EXT_SENSE = False
INITIAL_EXT_GUARD = False

#: What one graded point costs *besides* its settling time, in seconds: the
#: meter's setup block, then two readings, each of which drains its output
#: buffer, dwells and integrates.
#:
#: Back-calculated from a real run, then corrected for a fault that run was
#: paying for. The complete 45-point adjustment of 29 July 2026 took about fifty
#: minutes, which after subtracting settling, hookups and the two two-minute
#: memory reads left about forty seconds a point. But roughly four generator
#: commands are sent per point, and every one of them then cost a 2.04 s
#: throwaway read that the R6161 driver has since replaced with a
#: free serial poll -- eight seconds a point that is no longer spent.
#:
#: So: forty measured, less eight no longer paid, is about thirty-two, which is
#: two readings at roughly fourteen seconds plus the meter's setup block. The
#: next real run is the check on that.
POINT_OVERHEAD_S = 4.0 + 2 * 14.0

#: What a hookup group costs beyond its points: reading the connection, making
#: it, and setting the output limits the group needs.
HOOKUP_OVERHEAD_S = 60.0


#: How many significant figures a reading is shown to.
#:
#: Nine, because the reference meters this tool is used with are 8.5-digit
#: instruments: eight full digits and a leading half one, which is nine
#: significant figures. Showing fewer throws away resolution that was paid for
#: in integration time; showing more invents it.
READING_SIGNIFICANT = 9

#: SI value of one of each unit the engines display in.
SI_PER_UNIT: Dict[str, float] = {
    "V": 1.0, "mV": 1e-3, "uV": 1e-6, "nV": 1e-9,
    "A": 1.0, "mA": 1e-3, "uA": 1e-6, "nA": 1e-9,
}

_VOLT_PREFIXES = ((1.0, "V"), (1e-3, "mV"), (1e-6, "uV"), (1e-9, "nV"))
_AMP_PREFIXES = ((1.0, "A"), (1e-3, "mA"), (1e-6, "uA"), (1e-9, "nA"))


#: How many significant figures a reading is shown to.
#:
#: Nine, because the reference meters this tool is used with are 8.5-digit
#: instruments: eight full digits and a leading half one, which is nine
#: significant figures. Showing fewer throws away resolution that was paid for
#: in integration time; showing more invents it.
READING_SIGNIFICANT = 9

#: SI value of one of each unit the engines display in.


#: SI value of one of each unit the engines display in.
SI_PER_UNIT: Dict[str, float] = {
    "V": 1.0, "mV": 1e-3, "uV": 1e-6, "nV": 1e-9,
    "A": 1.0, "mA": 1e-3, "uA": 1e-6, "nA": 1e-9,
}

_VOLT_PREFIXES = ((1.0, "V"), (1e-3, "mV"), (1e-6, "uV"), (1e-9, "nV"))
_AMP_PREFIXES = ((1.0, "A"), (1e-3, "mA"), (1e-6, "uA"), (1e-9, "nA"))


def engineering_unit(reference, kind="V"):
    """Return ``(si_per_unit, unit)`` that puts ``reference`` in [1, 1000).

    ``reference`` is the magnitude the line should be scaled around -- normally
    the larger of the reading and its tolerance, so that a zero point reads in
    the same unit as the limit it is being judged against rather than in one
    three prefixes below it.

    The largest prefix is not exceeded: a 1000 V reading stays in volts rather
    than becoming 1 kV, because the range is called 1000 V and matching the
    front panel is worth more than one fewer digit.
    """
    table = _AMP_PREFIXES if kind == "I" else _VOLT_PREFIXES
    try:
        magnitude = abs(float(reference))
    except (TypeError, ValueError):
        magnitude = 0.0
    if not magnitude or not math.isfinite(magnitude):
        return table[0]
    for scale, unit in table:
        if magnitude >= scale:
            return scale, unit
    return table[-1]


def format_reading(value_si, scale=1.0, unit="V",
                   significant=READING_SIGNIFICANT):
    """Format one reading to ``significant`` figures, trailing zeros included.

    Trailing zeros are kept deliberately. A meter that returns 0.5000000 has
    measured seven zeros; printing ``0.5`` discards the statement that they were
    measured, and makes a reading look coarser than the instrument that produced
    it. This is a measurement, not a setting.
    """
    if value_si is None:
        return "-"
    try:
        scaled = float(value_si) / float(scale or 1.0)
    except (TypeError, ValueError, ZeroDivisionError):
        return "-"
    if not math.isfinite(scaled):
        return "-"
    if scaled == 0.0:
        return "%.*f" % (max(significant - 1, 0), 0.0)
    exponent = math.floor(math.log10(abs(scaled)))
    decimals = min(max(significant - 1 - int(exponent), 0), 15)
    return "%.*f" % (decimals, scaled)


def wait_with_progress(seconds, label="settling", width=34, tick=0.25):
    """Sleep for ``seconds``, drawing a progress bar that fills as it goes.

    Every deliberate wait in this tool goes through here. A run spends more of
    its wall-clock time waiting than doing anything else -- ten seconds a point,
    twenty on the 1000 V range, a minute on the 100 mA range and another on the
    divider hookup -- and a silent pause of that length is indistinguishable
    from a hung instrument. This makes the difference visible without the
    operator having to guess.

    The bar redraws in place with a carriage return, which also keeps it out of
    the run log: the unattended runner's tee writes only chunks that contain no
    carriage return, so the log records the work and not the waiting.

    Returns immediately for a non-positive duration, so a wait that has been
    configured away costs nothing and prints nothing.
    """
    total = float(seconds)
    if total <= 0:
        return
    started = time.monotonic()
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= total:
            break
        filled = int(width * elapsed / total)
        sys.stdout.write("\r      %-9s [%s%s] %5.1f s left "
                         % (label, "#" * filled, "." * (width - filled),
                            total - elapsed))
        sys.stdout.flush()
        time.sleep(min(tick, total - elapsed))
    sys.stdout.write("\r      %-9s [%s] %5.1f s      \n"
                     % (label, "#" * width, total))
    sys.stdout.flush()


#: Time allowed for thermoelectric EMF to settle after a hookup change, applied
#: to any group that asks for one. Only the divider group does: manual 7.2.2
#: directs that copper clips be used there and that one minute be allowed. At
#: 10 mV full scale a 1 microvolt thermal offset is 100 ppm, which is larger
#: than the tolerance the reading is graded against.
#:
#: Set to 0 to skip the wait entirely.
THERMAL_WAIT_S = 60
