"""Shared low-level core for the Advantest R6161 service tool.

The R6161 is a 1991 Z80-based DC voltage/current generator. Its calibration is
performed entirely over GPIB by a small set of commands that do not appear in the
Operation Manual; they were recovered by disassembling firmware A05 and are
documented in ``R6161_Technical_Reference.pdf``.

Architecture
------------
This module holds the hardware truth: the GPIB transport, the command
vocabulary, the calibration-block layout and the BCD codec. Workflow lives in
:mod:`perf_engine` / :mod:`adjust_engine`; presentation lives in the service
tool. Instrument-independent arithmetic (BCD, formatting, block decoding) is
kept as free functions outside :class:`R6161` so it can be exercised without an
instrument attached.

The transport fault
-------------------
This instrument has a defect that makes naive GPIB control fail almost
completely: **any command immediately preceded by a write-only command loses its
leading character.** ``RV4+0`` arrives as ``V4+0`` and raises Err.G15. A
calibration session is write-only from end to end, so without a workaround only
the first command of a session is received intact.

Measured on 2026-07-26, first-attempt no-retry ``*IDN?``::

    query only, nothing before        25/25 (100%)
    one write immediately before       2/25 (  8%)
    one write, then a 1.0 s pause      0/25 (  0%)
    two writes before                  2/25 (  8%)

A pause makes it *worse*, so this is transaction state, not settling. None of
the four block delimiters the manual permits avoids it, nor does disabling EOI::

    LF 2/20   CR LF 0/20   CR 2/20   EOI only 1/20   LF without EOI 1/20

The fix is :meth:`R6161._sacrifice` -- a throwaway read after every write. It is
the read *addressing* that clears the state, not the data, so a read that times
out works exactly as well as one that succeeds (20/20 either way). That is what
makes it usable during EXT CAL, where every query is gated off and the sacrifice
can only ever be a timeout.

Safety
------
``Z`` and ``*RST`` are never sent by this module and must never be sent by a
caller: handler ``$7FAE`` initialises the battery-backed program data, which the
Operation Manual 4.5.2(13) confirms discards calibration data.
"""

from __future__ import annotations

import math
import re
import sys
import time

# Reading display -- units, scaling, the progress bar -- is not specific to any
# instrument and lives in the core. It is re-exported here because the engines
# reach for these names through whichever driver module is loaded, so a plugin
# that did not offer them would fail the conformance check.
from units import (READING_SIGNIFICANT, SI_PER_UNIT, engineering_unit,
                   format_plain, format_reading, wait_with_progress)
from typing import Dict, List, Optional, Sequence, Tuple

try:
    import pyvisa
except ImportError:  # pyvisa is optional at import time; required to open a session.
    pyvisa = None


# --------------------------------------------------------------------------- #
# Transport tuning
# --------------------------------------------------------------------------- #
#: Timeout requested for the fallback throwaway read, in milliseconds.
#:
#: Requested, not observed. A read with nothing to read costs **2.04 s** on this
#: GPIB stack whatever this is set to -- the same fixed cost a serial poll of an
#: absent address pays, imposed by the driver below VISA rather than by the VISA
#: timeout. Measured on the bench: five reads after a write, 2.033 to 2.046 s,
#: with this at 600 ms. Technical Reference chapter 2 records the measurement.
#:
#: That number is why this read is now only a fallback. A serial poll does the
#: same job in 0.00 s; see :meth:`R6161._settle_talker`.
SACRIFICE_TIMEOUT_MS = 600

#: Default gap between commands. Manual A1.1.2 tabulates per-code execution
#: time as a handshake period plus an internal-processing period. The extremes
#: are RCL at 6.2 + 42 ms, VL/IL at 4.1 + 33 ms and MEM at 24.5 + 15.8 ms;
#: most commands are under 10 ms in total, so this is roughly seven times the
#: worst case and about fifty times the typical one.
#:
#: It is now very nearly the *whole* interval between one command reaching the
#: wire and the next -- the serial poll that follows a write costs 0.00 s. It
#: used to be this plus a 2 s throwaway read.
COMMAND_GAP_S = 0.5

#: Commands given an extra dwell afterwards. Measured on 2026-07-27, whether the
#: following command was answered after ``RCL``:
#:
#:     one sacrifice, 0.3 s gap     17 / 20   (85 %)
#:     one sacrifice, 1.5 s gap      9 / 10   (extra delay barely helps)
#:     two sacrifices, 0.3 s gap    29 / 30   (97 %)
#:
#: Read at the time as an unusually severe case of the transport fault. It is
#: not: ``RCL`` puts the instrument into program mode (see
#: RCL_ENTERS_PROGRAM_MODE below), where it stops answering, and the residual
#: failures are that mode change rather than any transport defect -- which is
#: consistent with the extra *reads* having helped where extra *waiting* did
#: not, since a read is what discovers that the instrument has stopped talking.
#: A caller that must be certain should send the escape command and verify.
#:
#: RCL is not used by either procedure; this is for the setup-memory features.
SLOW_COMMANDS = ("RCL",)

#: How long to dwell after one of those, in seconds.
SLOW_COMMAND_DWELL_S = 2.0

#: ``MEM`` clamps the voltage limit; it does not discard it. The parser rejects
#: anything above 1250 or with a non-zero low digit, then at $9061 clamps the
#: survivor to 130 unless the range code is 0 (the 1000 V range) or 8/9 with
#: option 01 fitted. Measured 2026-07-28 on the 1 V range: VL0100 -> 100,
#: VL0130 -> 130, VL0140 -> 130. The current limit is clamped in mirror image at
#: $90AD, to 13 mA, and only on range code 0 -- which is where the Operation
#: Manual's "IL13 on the 1000 V range" default comes from.
#:
#: An earlier version of this module reported the field as never stored. That
#: was wrong: every test had used a value above the clamp.
MEM_VL_CLAMP = 130
MEM_IL_CLAMP_1000V = 13
MEM_VL_MAX = 1250

#: The two limit fields are not independent: ``IL`` without ``VL`` raises G18
#: (internal code 31) and ``VL`` without ``IL`` raises G01.

#: The default record returned by ``MEM<nn>?`` for a channel that has never been
#: written. Used to check a channel against its factory state.
MEM_DEFAULT_RECORD = "V4, +0.000000 V,VL0130,IL125"

#: Per-channel setup storage. Each field has its own array indexed by channel;
#: the store at $9174 writes all four. Defaults are filled at $57A0 and $5830,
#: which is where VL0130 and IL125 come from.
MEM_ARRAYS = {
    "range":   (0xD9BA, 1),
    "sign":    (0xDBAE, 1),
    "value":   (0xD69A, 8),
    "vlimit":  (0xDA1E, 2),
    "ilimit":  (0xDAE6, 2),
}

#: ``RCL`` does not recall a setup -- it ENTERS PROGRAM MODE at that channel.
#: The handler sets $DDED at $8D03; the stored setup is applied when the program
#: runs. Measured 2026-07-28: $DDED read 0 before RCL00 and 1 after.
#:
#: While in program mode the instrument stops answering queries. That is a mode
#: change, not the transport fault. Send ``D`` with a value to return to direct
#: mode (measured to restore $DDED to 0), or issue a device clear, which also
#: stops any running program by way of $9330.
RCL_ENTERS_PROGRAM_MODE = True
RCL_ESCAPE_COMMAND = "D+0.000000"

#: Program mode IS enterable over the bus: RCL<nn> then STT. $DDF3 marks a run
#: in progress and cannot be read while one is, because MRB does not answer.
PROGRAM_MODE_FLAG = 0xDDED
PROGRAM_RUNNING_FLAG = 0xDDF3
PROGRAM_PAUSED_FLAG = 0xDDEF

#: ``ST`` stores the scan mode at $C0D2, and not as written:
#: ST0 -> 1 (single), ST1 -> 2 (repeat), ST2 -> 0 (step). Several handlers
#: branch on that byte, including the G20 gate, which is skipped when it is 0.
SCAN_MODE_ENCODING = {"ST0": 1, "ST1": 2, "ST2": 0}

#: Settling time after OPERATE before a reading is trusted. Both figures are the
#: manual's own, and they are given in two different places -- which is how the
#: second one came to be missed.
#:
#: Manual page 7-5, "before the Calibration Procedure", note 8:
#:
#:     "Set the settling time of approximately 5 seconds after output
#:      generation (except for the 1000 V range)."
#:
#: The exception is not left open. Manual page 7-15, footnote *1 against the
#: 1000 V range's own steps:
#:
#:     "Use the data displayed approximately 20 seconds after the OPERATE
#:      operation."
#:
#: An earlier version of this module carried 8.0 s for the 1000 V range and a
#: comment claiming the manual gave no figure for it. That was wrong on both
#: counts: the figure exists, it is 20 s, and 8 s is well short of it. The
#: measurement of 28 July 2026 agrees with the manual -- at 8 s the 1000 V range
#: was still 4.0 ppm low, and it does not reach 3 ppm until about 9 s.
#:
#: These three are **defaults**. The service tool carries a USER SETTINGS block
#: at the top of the file that assigns over them at startup, so an operator can
#: lengthen them for a noisy bench or a slow reference standard without editing
#: this module. Every consumer reads them through the module at the moment they
#: are needed -- ``r6161.SETTLE_S``, never ``from r6161 import SETTLE_S`` --
#: because a from-import binds the value at import time and would quietly ignore
#: the override.
#:
#: They are measurement requirements, not firmware ones: an entry made with no
#: deliberate delay after OPERATE was still accepted. Shortening them costs
#: accuracy rather than acceptance, which is exactly why getting them wrong is
#: hard to notice.
SETTLE_S = 5.0
SETTLE_HV_S = 20.0

#: Settling for the 100 mA range, which the Operation Manual singles out. Both
#: pages covering that range -- 7-27 and 7-28, steps (16)(1) and (16)(7) --
#: carry the line "The data about 60 seconds after the operation is follows",
#: and no other range's pages do. Taken at face value it asks for a minute
#: before the reading is used, six times the general figure, and the manual
#: gives no reason. The most likely one is self-heating: 100 mA into the
#: 10 ohm standard the same page specifies is 0.1 W, where every other point in
#: the procedure dissipates a thousandth of that.
#:
#: Unlike SETTLE_S and SETTLE_HV_S this one has NOT been confirmed by
#: measurement -- the settling study of 28 July covered the voltage ranges only.
#: It follows the manual because the manual is the better authority until
#: somebody measures it.
SETTLE_100MA_S = 60.0


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
#: throwaway read that :meth:`R6161._settle_talker` has since replaced with a
#: free serial poll -- eight seconds a point that is no longer spent.
#:
#: So: forty measured, less eight no longer paid, is about thirty-two, which is
#: two readings at roughly fourteen seconds plus the meter's setup block. The
#: next real run is the check on that.
POINT_OVERHEAD_S = 4.0 + 2 * 14.0

#: What a hookup group costs beyond its points: reading the connection, making
#: it, and setting the output limits the group needs.
HOOKUP_OVERHEAD_S = 60.0


_VOLT_PREFIXES = ((1.0, "V"), (1e-3, "mV"), (1e-6, "uV"), (1e-9, "nV"))
_AMP_PREFIXES = ((1.0, "A"), (1e-3, "mA"), (1e-6, "uA"), (1e-9, "nA"))


#: Time allowed for thermoelectric EMF to settle after a hookup change, applied
#: to any group that asks for one. Only the divider group does: manual 7.2.2
#: directs that copper clips be used there and that one minute be allowed. At
#: 10 mV full scale a 1 microvolt thermal offset is 100 ppm, which is larger
#: than the tolerance the reading is graded against.
#:
#: Set to 0 to skip the wait entirely.
THERMAL_WAIT_S = 60



# --------------------------------------------------------------------------- #
# Calibration memory geometry
#
# Three copies exist. Boot copies the EEPROM master over the working copy, so
# poking the working copy never persists; only the commit reached via REND
# writes the master.
# --------------------------------------------------------------------------- #
CAL_WORKING = 0xDC29      #: working copy in battery-backed RAM
CAL_MASTER = 0xE000       #: EEPROM master (an AT28-class part, not an alias)
CAL_DEFAULTS = 0x9AE9     #: factory defaults in ROM
CAL_BLOCK_LEN = 0x016C    #: 364 bytes

#: One-byte checksum immediately after the master block: the low eight bits of
#: the sum of all 364 bytes. The commit reached from REND copies the working
#: block to the master ($21A2) and then recomputes and stores this byte
#: ($402E). Power-up ($0241) and *TST? ($8815) both re-sum the block and compare
#: against it; a mismatch substitutes the ROM defaults and sets *TST? bit 3.
CAL_CHECKSUM_ADDR = 0xE16C


def cal_checksum(image: bytes) -> int:
    """Return the checksum byte the firmware stores for a calibration image."""
    if len(image) != CAL_BLOCK_LEN:
        raise ValueError("calibration image must be %d bytes, got %d"
                         % (CAL_BLOCK_LEN, len(image)))
    return sum(image) & 0xFF

#: Region offsets within the 364-byte block. 160 + 180 + 24 = 364 exactly.
FS_BASE, FS_COUNT, FS_SIZE = 0x000, 20, 8      #: full-scale coefficients
ZERO_BASE, ZERO_COUNT, ZERO_SIZE = 0x0A0, 20, 9  #: zero coefficients (8 BCD + flag)
RKT_BASE, RKT_COUNT, RKT_SIZE = 0x154, 3, 8    #: DAC and limit coefficients

#: The third array is not three DAC coefficients. Establishing which record is
#: which was done twice over: from firmware, where $43EB tests the voltage-limit
#: flag $DE3F and writes $DD8D while $4401 tests the current-limit flag $DE40
#: and writes $DD85; and on the bench on 27 July 2026, by entering each of the
#: three independently and watching only the expected record move.
#:
#: Only the low four bytes of each eight-byte slot are used; the upper four are
#: zero. The DAC coefficient is dimensionless and rounds; both limits are held
#: in hundredths of the point's entry unit and truncate.
RKT_RECORDS = (
    dict(name="DAC connection coefficient", selector="RKTA/RKTB/RKTD",
         unit="", divisor=1e6),
    dict(name="Current limit", selector="RIL", unit="mA", divisor=100.0),
    dict(name="Voltage limit", selector="RVL", unit="V", divisor=100.0),
)

#: Divisors. The full-scale records are the measured value in the record's own
#: unit; the zero records are a fraction of full scale.
FS_DIVISOR = 1e6
ZERO_DIVISOR = 1e10

#: The unit a full-scale record is stored in, and the value it holds on a
#: correctly adjusted instrument.
#:
#: This is **not** always the range's panel unit, and the exception is easy to
#: trip over: the 1 V range is *entered* in millivolts -- ``RD`` takes
#: ``1000.000`` there, and 1010.000 is the edge of the acceptance band -- but
#: its coefficient is *stored* in volts. Every other range stores in the same
#: unit it is entered in.
#:
#: Read from the EEPROM master of a working instrument on 26 July 2026::
#:
#:     1000V  1000.706147 V     1000mV  1000.769990 mV    100mA  100.132549 mA
#:      100V   100.063307 V      100mV   100.071350 mV     10mA   10.011873 mA
#:       10V    10.007850 V       10mV    10.008320 mV      1mA    1.000784 mA
#:        1V     1.001113 V   <-- volts, not millivolts
#:
#: Every record is within 0.14 % of its nominal, which is what makes the
#: 1 V range's three-decade offset unmistakable rather than a judgement call.
CAL_RECORD_FS: Dict[str, Tuple[float, str]] = {
    "1000V":  (1000.0, "V"),
    "100V":   (100.0,  "V"),
    "10V":    (10.0,   "V"),
    "1V":     (1.0,    "V"),
    "1000mV": (1000.0, "mV"),
    "100mV":  (100.0,  "mV"),
    "10mV":   (10.0,   "mV"),
    "100mA":  (100.0,  "mA"),
    "10mA":   (10.0,   "mA"),
    "1mA":    (1.0,    "mA"),
}

#: Record order within both the full-scale and zero arrays: the ten positive
#: points, then the ten negative points, each in this range order.
RANGE_ORDER = ("1000V", "100V", "10V", "1V", "1000mV", "100mV", "10mV",
               "100mA", "10mA", "1mA")


# --------------------------------------------------------------------------- #
# Range description
#
# ``code``   the V/I range-select code used in normal operation
# ``sel``    the RV/RI cal-point selector prefix used during EXT CAL
# ``fs``     full scale expressed in the range's panel unit
# ``unit``   the panel unit, which is also the unit RD expects
# ``si``     SI volts (or amps) per one panel unit
# ``divider`` True for the three Divider ranges, which emerge from their own
#            output terminals and cannot have a limit set (Err.G16)
#
# The panel unit is NOT the unit the ``D`` output command takes. See
# :meth:`R6161.set_output`.
# --------------------------------------------------------------------------- #
RANGES: Dict[str, dict] = {
    "10mV":   dict(code="V2", sel="RV2", fs=10.0,   unit="mV", si=1e-3, divider=True,  kind="V"),
    "100mV":  dict(code="V3", sel="RV3", fs=100.0,  unit="mV", si=1e-3, divider=True,  kind="V"),
    "1000mV": dict(code="V9", sel="RV9", fs=1000.0, unit="mV", si=1e-3, divider=True,  kind="V"),
    "1V":     dict(code="V4", sel="RV4", fs=1000.0, unit="mV", si=1e-3, divider=False, kind="V"),
    "10V":    dict(code="V5", sel="RV5", fs=10.0,   unit="V",  si=1.0,  divider=False, kind="V"),
    "100V":   dict(code="V6", sel="RV6", fs=100.0,  unit="V",  si=1.0,  divider=False, kind="V"),
    "1000V":  dict(code="V7", sel="RV7", fs=1000.0, unit="V",  si=1.0,  divider=False, kind="V"),
    "1mA":    dict(code="I1", sel="RI1", fs=1.0,    unit="mA", si=1e-3, divider=False, kind="I"),
    "10mA":   dict(code="I2", sel="RI2", fs=10.0,   unit="mA", si=1e-3, divider=False, kind="I"),
    "100mA":  dict(code="I3", sel="RI3", fs=100.0,  unit="mA", si=1e-3, divider=False, kind="I"),
}

#: The 1 V range is entered in mV during calibration but its full scale is 1 V.
#: ``fs_si`` is full scale in SI base units, which is what the accuracy
#: specification and the report are expressed against.
for _name, _r in RANGES.items():
    _r["fs_si"] = _r["fs"] * _r["si"]

#: Maximum digits in a numeric argument (manual 4.5.4). Excess is truncated, so
#: values must be formatted to fit rather than relying on the instrument.
MAX_DIGITS = 7

#: Commands that must never be sent. ``Z`` and ``*RST`` reach handler $7FAE,
#: which initialises battery-backed program data and discards calibration.
FORBIDDEN_COMMANDS = ("Z", "*RST")

# --------------------------------------------------------------------------- #
# The manual's standard resistors
#
# Instrument data, read by the core through ``active.driver``: which current
# ranges exist and what the Operation Manual specifies for measuring each one as
# a voltage drop. Another instrument has its own, or none at all.
# --------------------------------------------------------------------------- #
#: The three current ranges, in the order the manual works them.
CURRENT_RANGES: Tuple[str, ...] = ("1mA", "10mA", "100mA")

#: Standard-resistor values the Operation Manual specifies for the voltage-drop
#: method, one per current range: manual 7.2.2(11) for 1 mA, (13) for 10 mA and
#: (15) for 100 mA, pages 7-22, 7-25 and 7-27.
#:
#: Every one of them puts 1 V across the resistor at full scale, which is not a
#: coincidence: it lands each measurement at the top of the reference meter's
#: 1 V range, where the ppm-of-range term is smallest. A single 10 ohm standard
#: used for all three would read 10 mV at 1 mA, ten times down the meter's
#: 100 mV range, and would be worse than measuring the current directly.
MANUAL_SHUNTS: Dict[str, float] = {"1mA": 1000.0, "10mA": 100.0, "100mA": 10.0}


#: Used only when ``*IDN?`` has not been read, so that a certificate names a
#: maker rather than leaving the line blank. The instrument itself answers
#: ``ADVANTEST``.
DEFAULT_MANUFACTURER = "Advantest"


def _pretty_maker(text: str) -> str:
    """Render an all-capitals maker name as a name rather than a shout.

    ``*IDN?`` answers ``ADVANTEST``, which on a certificate beside typed
    laboratory and customer names reads as a mistake. Short strings are left
    alone: an acronym is upper case on purpose.
    """
    cleaned = (text or "").strip()
    if len(cleaned) > 3 and cleaned == cleaned.upper() and any(
            char.isalpha() for char in cleaned):
        return cleaned.title()
    return cleaned


# --------------------------------------------------------------------------- #
# GPIB error codes (Operation Manual A1.5.2)
#
# The displayed number is the internal code minus three. Mapping the displayed
# code back to its ROM site is what identified the transport fault: every error
# seen during a failed calibration was the *correct* response to the command
# minus its first character.
# --------------------------------------------------------------------------- #
GPIB_ERRORS: Dict[str, str] = {
    "G01": "The specified program code does not match the Generator definition.",
    "G02": "The string delimiter does not exist.",
    "G03": "An invalid channel number (other than 0 to 99) was specified.",
    "G04": "The last channel number is less than the first channel number.",
    "G05": "An invalid X value (other than 0, 1, 2 or 3) was specified for DLX.",
    "G06": "An invalid X value (other than 0 to 255) was specified for SMSX.",
    "G07": "An invalid X value (other than 2 to 7 or 1 to 3) was specified for VX or IX.",
    "G08": "An invalid X value (other than 10 to 1250) was specified for VLX.",
    "G09": "An invalid X value (other than 1 to 125) was specified for ILX.",
    "G10": "An invalid X value (other than 1 to 99) was specified for STMX.",
    "G11": "An invalid X value (other than 0, 1 or 2) was specified for STX.",
    "G12": "No data exists after the D code.",
    "G13": "An invalid data was specified after the D code.",
    "G15": "A remote control code was received during Ext. CAL.",
    "G16": "A VL/IL limit was set in the Divider mode.",
    "G17": "Data exceeding 400 characters was entered.",
    "G18": "An invalid format of data set was stored in memory.",
    "G20": "An invalid remote control code was received in Program mode.",
    "GAd": "An attempt was made to set 31 or more addresses in the GPIB "
           "Address Setup mode.",
}

#: Codes the firmware raises that do not appear in Operation Manual section
#: A1.5.2, which lists G01-G13, G15-G18 and G20 only. Both were confirmed on the
#: front panel of a working instrument on 27 July 2026.
#:
#: The displayed label is not an arithmetic function of the internal code. The
#: code is held at ``$DDCB`` and the panel label is selected through a jump
#: table at offset ``$68`` indexed by ``code - 1``; the tidy ``code - 3``
#: relation happens to hold over codes 4 to 24 and then stops.
UNDOCUMENTED_GPIB_ERRORS: Dict[str, str] = {
    "G14": "A calibration command was received while EXT CAL was disabled. "
           "Internal code 17; the counterpart of G15.",
    "G19": "An out-of-range EXT CAL data value was entered with RD. Internal "
           "code 33. The same condition entered from the front panel is "
           "reported as Err.P04; the handler selects between the two on the "
           "local/remote flag at $C0D3.",
}

#: Every code the tool can explain, documented or not.
ALL_GPIB_ERRORS: Dict[str, str] = dict(GPIB_ERRORS, **UNDOCUMENTED_GPIB_ERRORS)

#: Internal error code to displayed panel label, recovered from the jump table
#: at ROM ``$0068``. The dispatcher at ``$501D`` computes ``(code - 1) * 2``,
#: indexes that table, and jumps to a stub that loads the label's two digits as
#: ``tens * 256 + units`` and selects one of four group renderers. The group
#: decides the prefix letter, which is why the numbering is not monotonic in the
#: code: G16, G17 and G18 sit at codes 30, 29 and 31, and G20 at code 19.
#:
#: Two entries were confirmed on the front panel of a working instrument, with
#: the code read back from ``$DDCB``: code 17 displayed G14 and code 33
#: displayed G19. The remaining assignments follow from the same table, which
#: yields G01 to G20 exactly once each with no gaps or duplicates.
ERROR_CODE_LABELS: Dict[int, str] = {
    1: "P01", 2: "P02", 3: "P03",
    4: "G01", 5: "G02", 6: "G03", 7: "G04", 8: "G05", 9: "G06",
    10: "G07", 11: "G08", 12: "G09", 13: "G10", 14: "G11", 15: "G12",
    16: "G13", 17: "G14", 18: "G15", 19: "G20",
    20: "GAd",
    25: "b01", 26: "b02", 27: "b03", 28: "b04",
    29: "G17", 30: "G16", 31: "G18",
    32: "P04", 33: "G19", 34: "P05",
}

#: Codes 21 to 24 are the self-diagnostic messages of Operation Manual A1.5.4.
#: They carry text rather than a two-digit number, so they have no Err.xnn
#: label. Codes 21, 23 and 24 are raised from the boot path at $0220, $0278 and
#: $02A4, around the routine that loads the calibration master.
SELF_TEST_MESSAGES: Dict[int, str] = {
    21: "ROM sum check error. The display continues and the instrument does "
        "not proceed; the firmware halts in a loop at $0226.",
    22: "RAM read/write error. The display continues. No instruction in "
        "firmware A05 stores this code, so the message appears unreachable by "
        "the $DDCB path; the RAM test itself lives in the *TST? handler.",
    23: "Battery-backed RAM data destroyed. Displayed about five seconds, the "
        "RAM parameters are initialised and operation continues.",
    24: "Calibration data in the EEPROM destroyed. Displayed about five "
        "seconds, the ROM defaults are used and operation continues, out of "
        "adjustment.",
}

#: Codes that are not Err.xnn labels at all.
ERROR_CODES_NOT_LABELS = tuple(SELF_TEST_MESSAGES)


def label_for_code(code: int) -> str:
    """Return the panel label for an internal error code read from ``$DDCB``."""
    if code in SELF_TEST_MESSAGES:
        return "(self-test message: %s)" % SELF_TEST_MESSAGES[code].split(".")[0]
    return ERROR_CODE_LABELS.get(code, "(unknown code %d)" % code)


#: RD accepts a value within this fraction of the range's FULL SCALE of the
#: point's nominal value, and refuses anything outside with G19 over the bus or
#: P04 from the front panel. The reference is full scale, not the nominal: a
#: zero point has a nominal of zero, and its band is still +/-1 % of full scale.
#:
#: This applies to **range points only**. The five special points -- the three
#: DAC connection sub-steps and the two limits -- are gated by entirely
#: different tests; see :func:`rd_accepts_rkt` and :func:`rd_accepts_limit`.
#: An earlier version of this module assumed the +/-1 % band applied to the DAC
#: points as well, which would have refused an instrument's own measured output
#: at RKTB and made the manual's procedure impossible.
#:
#: The comparison is against a **decade nominal held in ROM**, not against
#: anything measured or stored. ``$52E8`` maps the armed range flag to an index
#: 0-3 through the ladders at ``$535E``/``$5399``, and that index picks one of
#: four constant pairs::
#:
#:     index 0   $9C85 / $9C8D     0.99 .. 1.01          nominal 1
#:     index 1   $9C95 / $9C9D     9.9  .. 10.1          nominal 10
#:     index 2   $9CA5 / $9CAD    99    .. 101           nominal 100
#:     index 3   $9CB5 / $9CBD   990    .. 1010          nominal 1000
#:
#: and for zero points a single one-sided limit at ``$9C65``/``$9C6D``/
#: ``$9C75``/``$9C7D`` -- 0.01, 0.1, 1, 10 -- which is 1 % of the same nominal.
#: The nominal in every case equals ``RANGES[name]["fs"]``, so the model below
#: is exact rather than approximate. Both comparisons are inclusive.
#:
#: A full-scale entry must also carry the point's own sign (``$5393`` requires a
#: ``+`` for a ``+F.S`` point, ``$53CE`` a ``-`` for ``-F.S``); zero points do
#: not check the sign at all.
#:
#: Measured on 27 July 2026, in each case by sweeping until the edge was found
#: to the last digit:
#:
#:   1 V range, +F.S     1010.000 mV accepted, 1010.001 refused
#:   1 V range, +F.S      990.000 mV accepted,  989.999 refused
#:   1 V range, zero       10.00000 mV accepted, 10.00001 refused
#:   10 mV divider, zero    0.100000 mV accepted, 0.100001 refused
#:   1 mA range, +F.S       1.010000 mA accepted, 1.010001 refused
#:   DAC points (10 V)     +/-0.100000 V accepted, 0.100001 refused
RD_ACCEPTANCE_FRACTION = 0.01


def rd_accepts(range_name: str, nominal_entry: float, value_entry: float) -> bool:
    """Report whether the firmware will accept ``value_entry`` at a point.

    Both values are in the point's entry unit. The band is
    ``RD_ACCEPTANCE_FRACTION`` of the range's full scale either side of the
    point's nominal, which is why a zero point -- nominal zero -- still has a
    usable band. Raising G19 costs a retry and lights the panel, so a caller is
    better off checking here first.
    """
    band = RD_ACCEPTANCE_FRACTION * RANGES[range_name]["fs"]
    deviation = abs(value_entry - nominal_entry)
    # A value exactly on the edge is accepted by the instrument, and binary
    # floating point puts some of those a fraction of an ulp outside the band
    # (1.010000 - 1.0 is not exactly 0.01). The tolerance is far below one
    # seven-digit entry step, so it cannot admit a value the firmware refuses.
    return deviation <= band or math.isclose(deviation, band, rel_tol=1e-9)


# --------------------------------------------------------------------------- #
# The five special calibration points have their own acceptance rules
#
# Nothing here shares the range points' +/-1 % band. All of it was traced in A05
# and every constant is a ROM immediate; none of these tests looks at the
# previously stored coefficient or at what the DAC was told to produce.
# --------------------------------------------------------------------------- #

#: Ceilings on the three DAC connection entries, from ``$52E8``'s RKT branch.
#: ``$53E6`` compares RKTA against ``$9C55`` = 0.100000; ``$5406`` and ``$543F``
#: compare RKTB and RKTD against ``$9C5D`` = 0.200000.
RKT_CEILING = {"RKTA": 0.100000, "RKTB": 0.200000, "RKTD": 0.200000}

#: The DAC sub-steps are also **ordered**: ``$5406`` requires RKTB to be
#: strictly greater than the RKTA value entered a moment earlier (kept at
#: ``$C03A``), and ``$543F`` requires RKTD to be strictly greater than RKTB
#: (``$C042``). Both must be entered positive. RKTA may be either sign.
#:
#: This is why the three have to be entered in order, in one pass, and why a
#: partial DAC entry is meaningless: the comparison chain is against the values
#: entered in the same sequence, and ``$4302``/``$433B`` then folds ``B - A``
#: and ``D - B`` into the single coefficient at ``$DD7D``.
RKT_ORDER = ("RKTA", "RKTB", "RKTD")

#: The two limit points are gated by a fixed window rather than by a percentage.
#: ``$3FF7`` requires the top four BCD digits to lie between ``$0400`` and
#: ``$0600`` inclusive (and the low digits to be zero when the top is exactly
#: ``$0600``). A negative entry is refused outright at ``$43F1``/``$4406``.
#:
#: Measured on hardware 29 July 2026, with the point energised: RVL refused
#: 380.0000, accepted 400.0000 and 500.0000, refused 620.0000; RIL refused
#: 38.00000, accepted 50.00000 and 60.00000, refused 62.00000. Both windows are
#: confirmed at both edges.
#:
#: **The digit-count rule is not enforced as described.** ``$3F9A`` was read as
#: requiring exactly three integer digits for ``RVL`` and two for ``RIL``, but
#: ``RD+0500.000`` and ``RD+050.0000`` were both accepted -- so leading zeros do
#: not break it, presumably because the parser normalises them away before
#: ``$C011`` is taken. The rule cannot be isolated from the window test by any
#: other entry, so it is left unenforced here rather than guessed at.
#:
#: These are +/-20 % windows about 500 V and 50 mA -- far wider than the range
#: points' +/-1 % -- which is consistent with the manual describing the outputs
#: only as "approximately 500 V" and "approximately 50 mA".
#:
#: ``(low, high, integer_digits, unit)``
LIMIT_ENTRY_WINDOW = {
    "RVL": (400.0, 600.0, 3, "V"),
    "RIL": (40.0, 60.0, 2, "mA"),
}


def integer_digit_count(text: str) -> int:
    """Return how many digits precede the decimal point in a formatted entry.

    This is what ``$C011`` holds after the parser at ``$9763`` has run, and the
    limit points are gated on it directly, so it has to be counted the same way
    the firmware does -- from the rendered text, not from the float.
    """
    body = text.lstrip("+-")
    whole = body.split(".")[0]
    return sum(character.isdigit() for character in whole)


def rd_accepts_rkt(selector: str, value: float,
                   previous: Optional[float] = None) -> Tuple[bool, str]:
    """Report whether ``RD`` will take ``value`` at one DAC connection point.

    ``previous`` is the value entered at the preceding sub-step -- RKTA's value
    when checking RKTB, RKTB's when checking RKTD -- because that is literally
    what the firmware compares against. Pass ``None`` to skip the ordering test.
    """
    if selector not in RKT_CEILING:
        raise KeyError("not a DAC connection point: %r" % selector)
    ceiling = RKT_CEILING[selector]

    if selector != "RKTA" and value <= 0:
        return False, ("%s must be entered positive; the firmware refuses a "
                       "negative value at $540F/$543F" % selector)
    if abs(value) > ceiling:
        return False, ("%s is limited to %.6f; %.6f exceeds it"
                       % (selector, ceiling, value))
    if previous is not None and selector != "RKTA":
        if value <= previous:
            return False, ("%s must be strictly greater than the previous "
                           "sub-step's %.6f, and %.6f is not"
                           % (selector, previous, value))
    return True, ""


def rd_accepts_limit(selector: str, value: float) -> Tuple[bool, str]:
    """Report whether ``RD`` will take ``value`` at ``RVL`` or ``RIL``.

    The value must be positive and inside the fixed window. Both windows were
    confirmed at both edges on hardware; the digit-count rule that was also
    read out of ``$3F9A`` was refuted there, so it is not applied. See the
    note on :data:`LIMIT_ENTRY_WINDOW`.
    """
    if selector not in LIMIT_ENTRY_WINDOW:
        raise KeyError("not a limit point: %r" % selector)
    low, high, digits, unit = LIMIT_ENTRY_WINDOW[selector]

    if value <= 0:
        return False, "%s must be entered positive" % selector
    if not low <= value <= high:
        return False, ("%s accepts %.4g to %.4g %s and %.6g is outside; an "
                       "instrument reading this far from nominal cannot be "
                       "adjusted at this point"
                       % (selector, low, high, unit, value))
    return True, ""


# --------------------------------------------------------------------------- #
# The serial-poll status byte -- the only channel that works during EXT CAL
#
# Everything the instrument can be asked is gated to EXT CAL = DISABLE, so for
# the whole of an adjustment run the instrument appears mute: an ``RD`` that the
# firmware refuses looks exactly like one it accepted. The IEEE-488 serial poll
# is the exception, and it is enough on its own.
#
# Firmware path, traced end to end in A05:
#
#   $4FCD  the single error-raise routine. All 21 sites that write the internal
#          error code at $DDCB call it on the very next instruction, so there is
#          no inline path around it.
#   $4FF7  for internal codes $04-$14 and $1D/$1E/$1F/$21 -- exactly the
#          "Err.Gnn" family, G19 included -- it falls to $5017.
#   $5E46  sets bit 1 of the status byte.
#   $5F09  composes $DDDF = (raw & mask $DDD7 & 0x17), with bit 6 (RQS) set
#          whenever any of those bits survives, and
#   $5F1A  writes the result to the TMS9914A serial-poll register at $F605.
#
# None of those five routines contains a ``CALL $93B0``, so none of it is gated.
# The SPAS handler $75CC explicitly refuses to drop RQS while bit 1 is set, and
# never clears bit 1 -- a poll is non-destructive.
#
# The bit is latched to the outcome of the most recently executed command line
# and cleared at $7725 when the NEXT line completes without error, which gives
# the pattern this module uses::
#
#     write("RV3+FS")   # clean line -> bit 1 cleared
#     write("RD1.0")    # executes in the ISR; a G19 sets bit 1 here
#     poll_status()     # 0x42 if refused, 0x00 if accepted
#
# Verified on the bench 2026-07-28 with EXT CAL in DISABLE, using errors that
# raise the same bit through the same routine::
#
#     baseline                                0x00
#     H            (clean)                    0x00
#     QQQ          (Err.G01, code 4)          0x42
#     H            (clean)                    0x00
#     V4           (clean)                    0x00
#     D+1000       (Err.G13, code $10)        0x42
#     poll again, twice, no command between   0x42, 0x42   <- not self-clearing
#     D+0.000000   (clean)                    0x00
# --------------------------------------------------------------------------- #

#: Bit 1 of the serial-poll byte: the most recent command line raised an error.
STB_ERROR_BIT = 0x02

#: Bit 6, RQS. Set alongside any unmasked status bit.
STB_RQS_BIT = 0x40

#: The mask at $DDD7 is ANDed in at *set* time ($5F09), so a masked-off bit is
#: never stored and cannot be recovered later. ``SMS`` is itself gated to
#: EXT CAL = DISABLE, so the mask must be opened up before the switch is moved.
SRQ_MASK_ALL = "SMS255"

#: Internal codes whose errors reach the status byte, from the compare chain at
#: $4FF7. Codes outside this set -- the boot self-tests $15/$17/$18, and $20,
#: the front-panel local twin of G19 -- raise no bit.
STB_REPORTING_CODES = tuple(range(0x04, 0x15)) + (0x1D, 0x1E, 0x1F, 0x21)


# --------------------------------------------------------------------------- #
# BCD codec and numeric formatting (free functions -- no instrument required)
# --------------------------------------------------------------------------- #
def bcd_decode(record: Sequence[int]) -> Optional[int]:
    """Decode a little-endian packed-BCD record to an integer.

    Each byte carries two decimal digits, least significant byte first. Returns
    ``None`` if any nibble is not a valid decimal digit, which is how a caller
    distinguishes a real record from uninitialised or mis-aligned memory.
    """
    value = 0
    for position, byte in enumerate(record):
        high, low = byte >> 4, byte & 0x0F
        if high > 9 or low > 9:
            return None
        value += (high * 10 + low) * (100 ** position)
    return value


def bcd_encode(value: int, length: int) -> bytes:
    """Encode a non-negative integer as a little-endian packed-BCD record.

    Provided for completeness and for round-trip testing of :func:`bcd_decode`;
    the tool never writes calibration bytes directly, because only the firmware
    commit reached via ``REND`` makes a change persist.
    """
    if value < 0:
        raise ValueError("packed BCD cannot represent a negative value")
    out = bytearray(length)
    for position in range(length):
        pair = value % 100
        value //= 100
        out[position] = ((pair // 10) << 4) | (pair % 10)
    if value:
        raise ValueError("value does not fit in %d BCD bytes" % length)
    return bytes(out)


def format_entry(value: float, max_digits: int = MAX_DIGITS) -> str:
    """Format a measured value for ``RD``/``D``, with an explicit sign.

    The manual caps numeric data at seven characters and silently truncates the
    excess, so the value is rendered with as many decimal places as will fit
    rather than being allowed to overflow. Leading zeros are counted as digits,
    which is the conservative reading of the specification.
    """
    magnitude = abs(value)
    text = "%.0f" % magnitude
    for decimals in range(9, -1, -1):
        candidate = "%.*f" % (decimals, magnitude)
        if sum(character.isdigit() for character in candidate) <= max_digits:
            text = candidate
            break
    return ("-" if value < 0 else "+") + text


def parse_number(text: str) -> float:
    """Extract the first floating-point number from an instrument response."""
    match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text)
    if not match:
        raise ValueError("no number in response: %r" % text)
    return float(match.group(0))


# --------------------------------------------------------------------------- #
# Calibration block decoding
# --------------------------------------------------------------------------- #
def decode_cal_block(image: bytes) -> Dict[str, list]:
    """Decode a 364-byte calibration image into its three record arrays.

    Returns a dict with three lists of row dicts:

    ``fs``    twenty full-scale coefficients, ``value`` in the range's panel unit
    ``zero``  twenty zero coefficients, ``value`` as a fraction of full scale
    ``rkt``   three DAC connection coefficients, reported as raw BCD integers

    A record whose bytes are not valid BCD is reported with ``value=None`` and
    its raw bytes preserved, so a corrupt image is described rather than
    rejected.
    """
    if len(image) != CAL_BLOCK_LEN:
        raise ValueError("calibration image must be %d bytes, got %d"
                         % (CAL_BLOCK_LEN, len(image)))
    out: Dict[str, list] = {"fs": [], "zero": [], "rkt": []}

    for index in range(FS_COUNT):
        offset = FS_BASE + index * FS_SIZE
        raw = image[offset:offset + FS_SIZE]
        digits = bcd_decode(raw)
        out["fs"].append(dict(
            index=index, offset=offset, address=CAL_WORKING + offset,
            range=RANGE_ORDER[index % 10],
            polarity="+" if index < 10 else "-",
            point="+F.S" if index < 10 else "-F.S",
            raw=raw, value=(None if digits is None else digits / FS_DIVISOR)))

    for index in range(ZERO_COUNT):
        offset = ZERO_BASE + index * ZERO_SIZE
        raw = image[offset:offset + ZERO_SIZE]
        digits = bcd_decode(raw[:8])
        out["zero"].append(dict(
            index=index, offset=offset, address=CAL_WORKING + offset,
            range=RANGE_ORDER[index % 10],
            polarity="+" if index < 10 else "-",
            point="+0" if index < 10 else "-0",
            flag=raw[8], raw=raw,
            value=(None if digits is None else digits / ZERO_DIVISOR)))

    for index in range(RKT_COUNT):
        offset = RKT_BASE + index * RKT_SIZE
        raw = image[offset:offset + RKT_SIZE]
        digits = bcd_decode(raw[:4])
        meta = RKT_RECORDS[index]
        out["rkt"].append(dict(
            index=index, offset=offset, address=CAL_WORKING + offset,
            name=meta["name"], selector=meta["selector"], unit=meta["unit"],
            raw=raw, digits=digits,
            value=(None if digits is None else digits / meta["divisor"])))

    return out


def validate_cal_block(image: bytes) -> List[str]:
    """Return a list of problems found in a calibration image; empty is good.

    This is the read-back check for a commit. It is deliberately paranoid,
    because the failure it exists to catch is a *plausible-looking* block: the
    instrument keeps working, the panel shows no error, and the damage is only
    visible the next time someone measures the instrument.

    Four things are checked:

    1. **Length.** A short read means the transfer failed, not that the block is
       short.
    2. **BCD validity.** Every record must decode. A record that is not BCD is
       one the firmware will read as garbage.
    3. **A full-scale coefficient of zero.** No range legitimately has a
       full-scale coefficient of zero -- it would mean the range produces no
       output at all -- so a zeroed record is the signature of a point that was
       committed from a dead range or a refused entry.
    4. **A full-scale coefficient wildly away from its range's nominal.** The
       firmware's own +/-1 % gate should make this impossible, so anything
       outside +/-10 % means the commit did not land the way it was entered.

    The stored checksum is *not* checked here, because it lives one byte past
    the end of the block; see :data:`CAL_CHECKSUM_ADDR` and :func:`cal_checksum`.
    """
    problems: List[str] = []
    if len(image) != CAL_BLOCK_LEN:
        return ["image is %d bytes, expected %d" % (len(image), CAL_BLOCK_LEN)]

    decoded = decode_cal_block(image)

    for row in decoded["fs"]:
        label = "%s %s ($%04X)" % (row["range"], row["point"], row["address"])
        if row["value"] is None:
            problems.append("%s: not valid BCD (%s)" % (label, row["raw"].hex()))
            continue
        if row["value"] == 0.0:
            problems.append("%s: full-scale coefficient is zero, which no "
                            "working range has" % label)
            continue
        nominal, unit = CAL_RECORD_FS[row["range"]]
        if not 0.9 * nominal <= row["value"] <= 1.1 * nominal:
            problems.append("%s: %.6g is more than 10 %% from the nominal "
                            "%.6g %s" % (label, row["value"], nominal, unit))

    for row in decoded["zero"]:
        label = "%s %s ($%04X)" % (row["range"], row["point"], row["address"])
        if row["value"] is None:
            problems.append("%s: not valid BCD (%s)" % (label, row["raw"].hex()))

    for row in decoded["rkt"]:
        if row["value"] is None:
            problems.append("%s ($%04X): not valid BCD (%s)"
                            % (row["name"], row["address"], row["raw"].hex()))

    return problems


def compare_cal_blocks(before: bytes, after: bytes) -> List[dict]:
    """Return one row per record that differs between two calibration images.

    Used to confirm that a commit landed and to show the operator exactly what
    moved. Rows carry both decoded values so the caller can present them in
    whatever unit suits.
    """
    old, new = decode_cal_block(before), decode_cal_block(after)
    rows: List[dict] = []
    for kind in ("fs", "zero", "rkt"):
        for a, b in zip(old[kind], new[kind]):
            if a["raw"] != b["raw"]:
                rows.append(dict(kind=kind,
                                 label=a.get("selector") or
                                 ("%s %s" % (a["range"], a["point"])),
                                 address=a["address"],
                                 before=a["value"], after=b["value"]))
    return rows


# --------------------------------------------------------------------------- #
# Live instrument session
# --------------------------------------------------------------------------- #
class R6161:
    """A live GPIB session with an Advantest R6161.

    Every write goes through :meth:`write`, which issues the sacrificial read
    described in the module docstring. Callers should not reach past this class
    to the VISA session, because a raw write breaks the next command.

    The EXT CAL switch is a physical rear-panel switch read at port ``$FEC0``;
    no command can set it, so the calibration methods can only report what they
    find and rely on the operator.
    """

    def __init__(self, resource: str = "GPIB0::4::INSTR", timeout_ms: int = 2500,
                 send_ifc: bool = True) -> None:
        if pyvisa is None:
            raise RuntimeError("pyvisa is required to open an instrument session "
                               "(pip install pyvisa)")
        self.resource = resource
        self.rm = pyvisa.ResourceManager()
        if send_ifc:
            self.interface_clear()
        self.dev = self.rm.open_resource(resource)
        self.dev.timeout = timeout_ms
        self.dev.read_termination = "\n"
        self.dev.write_termination = "\n"
        self._timeout_ms = timeout_ms
        #: Set once by :meth:`identify`; the R6161 does implement ``*IDN?``
        #: (entry 61, handler ``$86C2``, response string at ``$9DE5``) despite
        #: it being absent from the Operation Manual's query table.
        self.idn = ""
        self.manufacturer = ""
        self.model = "R6161"
        self.firmware = ""
        #: Neither of these is in ``*IDN?`` -- the R6161 predates the convention
        #: that puts them there -- so they stay blank until an operator supplies
        #: them for the certificate. Nothing this module does depends on them.
        self.serial = ""
        self.options = ""
        # IFC and REN were asserted a moment ago, and REN really does leave the
        # next command a character short -- 0/5 without this, 5/5 with it. A
        # serial poll clears it, so the first command of the session lands.
        self._settle_talker()

    # ---- bus level ---------------------------------------------------------- #
    def interface_clear(self, board: str = "GPIB0::INTFC") -> bool:
        """Assert IFC and remote enable on the interface.

        A wedged bus is a separate fault from the transport defect and is
        cleared by IFC. Failure is non-fatal: some VISA backends do not expose
        an interface resource.
        """
        try:
            interface = self.rm.open_resource(board)
            interface.timeout = 3000
            interface.send_ifc()
            time.sleep(0.3)
            try:
                interface.control_ren(1)
            except Exception:
                pass
            interface.close()
            time.sleep(0.4)
            return True
        except Exception:
            return False

    def _settle_talker(self) -> None:
        """Make the instrument ready to be read from again. Costs nothing.

        Something has to come between a command and the next query on this
        instrument, or the query times out. That much was always true; what it
        had to be was never measured, and the answer turns out to be very cheap.

        **A serial poll.** It addresses the instrument to talk and releases it,
        which is apparently all that is needed, and a poll of a device that is
        present returns in 0.00 s.

        The evidence, all of it measured on the bench and recorded in chapter 2
        of the Technical Reference, in the order it was found:

        * With *nothing* between, a query after a command fails on every command
          the driver sends: ``SEN0`` 0/5, ``V5`` 0/5, ``E`` 0/5, ``H`` 0/5,
          ``VL`` 0/5, ``IL`` 0/5, ``SMS255`` 0/5. Not one command in particular.
          All of them.
        * 45 trials across nine commands, three ways:

          ==================  ==========  ================  =========
          Between             Commands    REN boundary      Cost each
          ==================  ==========  ================  =========
          nothing             40/45       0/5               0.00 s
          **serial poll**     **45/45**   **5/5**           **0.00 s**
          throwaway read      45/45       5/5               2.04 s
          ==================  ==========  ================  =========

        The poll covers the REN boundary too -- the one real character-loss case,
        where a command sent straight after asserting REN arrives a character
        short (``*IDN?`` becoming ``IDN?``, which lands on the ``I`` handler and
        raises Err.G07). So one mechanism serves both.

        What this replaces was a throwaway read, taken after every write, on the
        theory that a write corrupted the command after it. It did not: write
        then write, and write then ``*IDN?``, are both clean 5/5 with nothing
        between. The read was silently doing this
        job instead, and doing it for 2.04 s a time -- a fruitless read costs
        that on this GPIB stack however short ``SACRIFICE_TIMEOUT_MS`` is set.
        Across an adjustment's 150 commands and a verification's 200 that is
        about five minutes a run, and removing it without understanding what it
        was for broke ``arm_error_reporting`` and nothing else, which is how the
        real reason came to light.

        :meth:`_sacrifice` is kept as the fallback for a bus where the poll
        itself will not answer.
        """
        try:
            self.dev.read_stb()
        except Exception:
            self._sacrifice()

    def _sacrifice(self) -> None:
        """The old throwaway read, kept as a fallback for :meth:`_settle_talker`.

        A bare read rather than a query, because it sends no command and so can
        raise nothing itself. Reached only when a serial poll fails, which on a
        healthy bus does not happen.
        """
        previous = self.dev.timeout
        self.dev.timeout = SACRIFICE_TIMEOUT_MS
        try:
            self.dev.read()
        except Exception:
            pass
        finally:
            self.dev.timeout = previous

    # ---- primitives --------------------------------------------------------- #
    def write(self, command: str, gap: float = COMMAND_GAP_S) -> None:
        """Send one command, then settle the talker.

        This is the only sanctioned way to send a command. ``Z`` and ``*RST``
        are refused outright.

        The settle is a serial poll and costs nothing; without it the next query
        on this instrument times out. See :meth:`_settle_talker` for why, and
        for what it replaced.
        """
        if command.strip().upper() in FORBIDDEN_COMMANDS:
            raise ValueError("refusing to send %r: it discards calibration data "
                             "(handler $7FAE)" % command)
        time.sleep(gap)
        self.dev.write(command)
        self._settle_talker()
        # A few commands change the instrument's mode rather than just their own
        # setting, and it stops answering while they do; see SLOW_COMMANDS. That
        # needs waiting out, which a serial poll does not do.
        if command.strip().upper().startswith(SLOW_COMMANDS):
            time.sleep(SLOW_COMMAND_DWELL_S)

    def query(self, command: str, gap: float = COMMAND_GAP_S) -> str:
        """Send a query and return the response, stripped.

        A query is self-synchronising -- it ends in a read -- so it needs no
        sacrifice of its own. Every query is gated to EXT CAL = DISABLE and will
        time out while a calibration is in progress.
        """
        time.sleep(gap)
        return self.dev.query(command).strip()

    # ---- the serial-poll acceptance channel --------------------------------- #
    def poll_status(self) -> Optional[int]:
        """Return the IEEE-488 serial-poll status byte, or ``None`` if it fails.

        The poll is a bus operation served by the TMS9914A, not a command, so it
        answers with the EXT CAL switch in either position -- which makes it the
        only way to see anything at all during a calibration run. It does not
        clear the byte; see the module-level notes on :data:`STB_ERROR_BIT`.
        """
        try:
            return int(self.dev.read_stb())
        except Exception:
            return None

    def line_errored(self) -> Optional[bool]:
        """Did the most recent command line raise an error?

        ``True``/``False`` from bit 1 of the status byte; ``None`` if the poll
        itself failed, which a caller must not read as "accepted".
        """
        status = self.poll_status()
        if status is None:
            return None
        return bool(status & STB_ERROR_BIT)

    def arm_error_reporting(self) -> bool:
        """Open the SRQ mask so command errors reach the status byte.

        The mask at ``$DDD7`` is applied when a bit is *set* (``$5F09``), so a
        bit masked off at the time of the error is lost for good. ``SMS`` is
        gated to EXT CAL = DISABLE, so this has to be done before the switch is
        moved to ENABLE -- after that it is too late.

        The power-on default is ``255``, but the routine that sets it
        (``$9396``) is only reached from ``$2CE3`` when ``$C0D3 == 2``, so it is
        not safe to assume. Returns ``True`` if the mask reads back as ``255``.
        """
        self.write(SRQ_MASK_ALL)
        try:
            return "255" in self.query("SMS?")
        except Exception:
            return False

    def write_verified(self, command: str,
                       gap: float = COMMAND_GAP_S) -> Optional[bool]:
        """Send a command and report whether the firmware accepted it.

        Returns ``True`` if the line executed cleanly, ``False`` if it raised an
        error, and ``None`` if the status byte could not be read. Nothing may be
        sent to the instrument between the write and the poll, because the next
        clean line is what clears the bit.
        """
        self.write(command, gap=gap)
        errored = self.line_errored()
        if errored is None:
            return None
        return not errored

    def close(self) -> None:
        """Close the VISA session; never raises."""
        try:
            self.dev.close()
        except Exception:
            pass

    # ---- identity ----------------------------------------------------------- #
    def identify(self) -> str:
        """Query and cache ``*IDN?``. Returns the raw response.

        The response has the form ``ADVANTEST,R6161,REV A05``, and every field
        of it is unpacked here so that a certificate can state the instrument's
        identity from the instrument rather than from anything typed. There is
        no serial number and no option list in it, so those two stay blank
        unless the operator supplies them.
        """
        self.idn = self.query("*IDN?")
        parts = [p.strip() for p in self.idn.split(",")]
        if parts and parts[0]:
            self.manufacturer = _pretty_maker(parts[0])
        if len(parts) >= 2 and parts[1]:
            self.model = parts[1]
        if len(parts) >= 3 and parts[-1]:
            # "REV A05" -> "A05". The revision is what identifies the firmware;
            # the word in front of it is a label, and repeating it on a
            # certificate line already headed "Firmware" says nothing.
            self.firmware = re.sub(r"^REV\.?\s*", "", parts[-1],
                                   flags=re.IGNORECASE).strip() or parts[-1]
        return self.idn

    @property
    def description(self) -> str:
        """What the instrument is, for the certificate's item-under-test block.

        The maker comes from ``*IDN?``; what follows is the instrument's own
        description of itself and is the same for every R6161.
        """
        return "%s Voltage Current Source" % (self.manufacturer
                                             or DEFAULT_MANUFACTURER)

    def ext_cal_enabled(self) -> Optional[bool]:
        """Report the EXT CAL switch position. ``None`` means genuinely unknown.

        ``*IDN?`` is gated (``CALL $93B0`` / ``JP nz,$9985``), so the two
        positions give two distinguishable outcomes rather than one outcome and
        one silence:

        * **DISABLE** -- the query answers, and the line was clean, so the
          status byte's error bit is clear.
        * **ENABLE** -- the query is refused with Err.G15, internal code ``$12``,
          which is inside the reporting range at ``$4FF7``; the read times out
          but the poll comes back with bit 1 set.

        A timeout *and* a clear bit is neither: the instrument is off, wrongly
        addressed, or the bus is wedged. That case returns ``None``, because
        reporting it as ENABLE -- which an earlier version of this method did --
        invites a caller to proceed into a calibration against a dead bus.
        """
        answered = False
        try:
            answered = "R6161" in self.query("*IDN?").upper()
        except Exception:
            pass
        errored = self.line_errored()
        if answered and errored is False:
            return False
        if errored is True:
            return True
        if answered:
            # Answered, but the poll failed. The answer is the stronger signal.
            return False
        return None

    # ---- normal operation --------------------------------------------------- #
    def standby(self) -> None:
        """Put the output in STANDBY (``H``). De-energises the terminals."""
        self.write("H")

    def operate(self) -> None:
        """Put the output in OPERATE (``E``). Energises the terminals."""
        self.write("E")

    def select_range(self, range_name: str) -> None:
        """Select an output range by its catalogue name (e.g. ``"10V"``)."""
        self.write(RANGES[range_name]["code"])

    def set_output(self, value_si: float) -> None:
        """Set the output value, given in SI base units (volts or amps).

        With the unit suffix omitted the ``D`` command takes **volts** (or
        milliamps for the current ranges) regardless of the selected range --
        the manual's own example is ``V6`` then ``D+30`` for 30 V. This is a
        different convention from the calibration entry unit: the 1 V range is
        calibrated in mV but ``D+1000`` on that range means 1000 V and is
        rejected with Err.G13.
        """
        if value_si == 0.0:
            self.write("D+0")
        else:
            self.write("D" + format_entry(value_si))

    def set_output_on_range(self, range_name: str, value_si: float) -> None:
        """Set the output with ``D``, in the unit that range's ``D`` expects.

        **``D`` does not take one unit for every range.** It takes the unit the
        selected range displays in, and the Divider ranges display in
        millivolts. Sending an SI value on those puts out a thousandth of what
        was asked for -- a full-scale point that measures as a zero, which looks
        like a dead instrument rather than a wrong command.

        Established on 29 July 2026 from the instrument's own readback, with the
        output in STANDBY so nothing had to be connected:

        ==========  =========  ==================  ==============
        Range       Sent       Readback            D is in
        ==========  =========  ==================  ==============
        10 V        ``D+10``   ``+10.00000 V``     volts
        1 V         ``D+1``    ``+1.000000 V``     volts
        10 mV       ``D+0.01`` ``+00.01000 MV``    **millivolts**
        10 mV       ``D+10``   ``+10.00000 MV``    **millivolts**
        100 mV      ``D+100``  ``+100.0000 MV``    **millivolts**
        1000 mV     ``D+1000`` ``+1000.000 MV``    **millivolts**
        ==========  =========  ==================  ==============

        Current ranges take milliamps, which is what
        :meth:`set_output_current_mA` has always done.
        """
        spec = RANGES[range_name]
        if spec["kind"] == "I":
            self.set_output_current_mA(value_si / 1e-3)
        elif spec.get("divider"):
            millivolts = value_si / 1e-3
            self.write("D+0" if millivolts == 0.0
                       else "D" + format_entry(millivolts))
        else:
            self.set_output(value_si)

    def set_output_current_mA(self, milliamps: float) -> None:
        """Set a current output, given in milliamps."""
        self.write("D+0" if milliamps == 0.0 else "D" + format_entry(milliamps))

    def set_voltage_limit(self, volts: int) -> None:
        """Set the DC voltage limit ``VLx``, 10 to 1250 V in 10 V steps.

        **The initial value is VL130, and at that setting the 1000 V range
        produces no output at all** when it is driven by the ``D`` data command,
        while its zero point still reads correctly -- so range select and
        OPERATE both appear to work and only a full-scale measurement reveals
        the fault. Any procedure that sets the 1000 V range's output with ``D``
        must raise this first.

        It is **not** needed during calibration: there the firmware drives the
        output from the selected point rather than through ``D``, and the limit
        does not gate that path.

        The handler at ``$7D1C`` is gated to EXT CAL = DISABLE, so the limit
        can only be set with the switch in that position; it cannot be set in
        Divider mode at all (Err.G16).
        """
        self.write("VL%d" % int(volts))

    def set_current_limit(self, milliamps: int) -> None:
        """Set the DC current limit ``ILx``, 1 to 125 mA in 1 mA steps.

        The default is IL125, but IL13 on the 1000 V range and on the 1 mA /
        10 mA ranges of option 01.
        """
        self.write("IL%d" % int(milliamps))

    def set_sense(self, external: bool) -> None:
        """Select external (``SEN1``) or internal (``SEN0``) sense.

        Manual 7.2.2(1) calibrates with the DMM connected straight across the
        output terminals, i.e. internal sense, so a calibration run selects
        internal -- and leaves it there when it finishes. See
        :data:`INITIAL_EXT_SENSE`.
        """
        self.write("SEN1" if external else "SEN0")

    def set_guard(self, external: bool) -> None:
        """Select external (``GRD1``) or internal (``GRD0``) guard."""
        self.write("GRD1" if external else "GRD0")

    # ---- setup memory -------------------------------------------------------- #
    def read_memory_channel(self, channel: int) -> str:
        """Return the stored setup for one channel, e.g.::

            MEM00,V4, +0.000000 V,VL0130,IL125

        Query one channel at a time. The range form ``MEM<first>,<last>?``
        returns up to a hundred records in a single response and has been seen
        to destabilise the session; see :data:`MEM_DEFAULT_RECORD` for the
        factory value and :data:`MEM_VL_CLAMP` for the limit clamp that
        these queries expose.
        """
        return self.query("MEM%02d?" % channel).strip()

    def memory_channel_is_default(self, channel: int) -> bool:
        """True if the channel still holds its factory setup."""
        return self.read_memory_channel(channel).endswith(MEM_DEFAULT_RECORD)

    # ---- calibration -------------------------------------------------------- #
    def select_cal_point(self, selector: str) -> None:
        """Select a calibration point.

        ``selector`` is one of the point-selecting commands recovered from
        firmware: ``RV<d>{+|-}{0|FS}``, ``RI<d>{+|-}{0|FS}``, ``RKTA``/``RKTB``/
        ``RKTD``, ``RVL`` or ``RIL``. None of them takes a value -- the value is
        supplied separately by :meth:`enter_value` -- and all are gated to
        EXT CAL = ENABLE.
        """
        self.write(selector)

    def enter_value(self, value: float) -> str:
        """Enter a measured value for the selected point (``RD``).

        The value must already be expressed in the point's entry unit: the
        range's panel unit for range points (so the 1 V range is entered in mV),
        volts for the DAC and voltage-limit points, and milliamps for the
        current-limit point. Returns the command text that was sent, so the
        caller can log exactly what the instrument received.

        Prefer :meth:`enter_value_verified`: a value outside the +/-1 % band is
        refused with Err.G19 and this method cannot tell you that it happened.
        """
        command = "RD" + format_entry(value)
        self.write(command)
        return command

    def enter_value_verified(self, value: float) -> Tuple[str, Optional[bool]]:
        """Enter a value and confirm the firmware took it.

        Returns ``(command, accepted)``. ``accepted`` is ``True`` if the entry
        was taken, ``False`` if it was refused -- Err.G19, internal code ``$21``,
        raised at ``$53D4``/``$4440`` for a value outside the +/-1 % window
        tested at ``$5495`` -- and ``None`` if the status byte could not be read,
        which is not the same as success and must not be treated as such.

        This is the only feedback available during a run. Every query is gated
        to EXT CAL = DISABLE, so without the poll a refused entry is silent and
        the point simply keeps its old coefficient while the operator's table
        shows a value that was never stored.

        The instrument must be addressed as a listener in REMOTE for the code to
        be ``$21`` rather than ``$20`` (the check on ``$C0D3``); a command
        arriving over the bus always is.
        """
        command = "RD" + format_entry(value)
        self.write(command)
        errored = self.line_errored()
        if errored is None:
            return command, None
        return command, not errored

    def cal_end(self, settle_s: float = 8.0) -> Optional[bool]:
        """Send ``REND`` (CAL END), which commits every coefficient to EEPROM.

        This is the only operation that makes a calibration change persist.
        Entries accumulate in RAM until it is sent, so abandoning a run before
        this point leaves the instrument exactly as it was. The panel shows
        "CAL End" blinking for about five seconds while the write completes.

        Returns whether the line was accepted, from the status byte: ``True``
        committed, ``False`` refused, ``None`` unknown. The poll is taken before
        the settle so that it reflects ``REND`` itself rather than anything the
        commit does afterwards.
        """
        self.write("REND")
        accepted = self.line_errored()
        wait_with_progress(settle_s, label="CAL END")
        if accepted is None:
            return None
        return not accepted

    # ---- calibration memory read ------------------------------------------- #
    def read_byte(self, address: int, retries: int = 10) -> Optional[int]:
        """Read one byte of instrument memory with ``MRB``.

        ``MRB`` is undocumented and is gated to EXT CAL = DISABLE, so the
        calibration block can only be examined before or after a run, never
        during one. Returns ``None`` if every attempt fails.
        """
        for _ in range(retries):
            try:
                response = self.query("MRB%X?" % address, gap=0.15)
                hex_digits = "".join(c for c in response if c in "0123456789ABCDEFabcdef")
                if len(hex_digits) >= 2:
                    return int(hex_digits[:2], 16)
            except Exception:
                time.sleep(0.15)
        return None

    def read_block(self, start: int, length: int, progress=None) -> Optional[bytes]:
        """Read ``length`` bytes from ``start`` one byte at a time.

        ``progress`` is an optional ``callable(done, total)`` for a progress
        bar. Returns ``None`` if any byte could not be read, because a partial
        calibration image is more dangerous than no image at all.
        """
        out = bytearray()
        for index in range(length):
            value = self.read_byte(start + index)
            if value is None:
                return None
            out.append(value)
            if progress is not None:
                progress(index + 1, length)
        return bytes(out)

    def read_cal_image(self, master: bool = False, progress=None) -> Optional[bytes]:
        """Read the whole 364-byte calibration block.

        ``master=True`` reads the EEPROM master at ``$E000`` instead of the
        working copy at ``$DC29``. The two agree except between a change and the
        next boot, so comparing them is a cheap integrity check.
        """
        return self.read_block(CAL_MASTER if master else CAL_WORKING,
                               CAL_BLOCK_LEN, progress=progress)

    def read_cal_checksum(self) -> Optional[int]:
        """Read the stored checksum byte that guards the EEPROM master.

        The commit reached from ``REND`` copies the working block to the master
        (``$21A2``) and then recomputes this byte (``$402E``). Power-up
        (``$0241``) and ``*TST?`` (``$8815``) re-sum the block and compare; on a
        mismatch the firmware **substitutes the ROM defaults**, which is a silent
        loss of the whole calibration at the next power cycle rather than at the
        moment of the commit. Checking it immediately after a commit is what
        turns that into an error the operator sees now.
        """
        return self.read_byte(CAL_CHECKSUM_ADDR)

    def verify_cal_commit(self, progress=None) -> Tuple[Optional[bytes], List[str]]:
        """Read the EEPROM master back and check it is internally consistent.

        Returns ``(image, problems)``. ``image`` is ``None`` if the read failed,
        which is itself reported as a problem rather than passed off as success.
        Requires EXT CAL = DISABLE, because ``MRB`` is gated.
        """
        image = self.read_cal_image(master=True, progress=progress)
        if image is None:
            return None, ["the calibration memory could not be read back, so "
                          "the commit is unverified (MRB needs EXT CAL = "
                          "DISABLE)"]
        problems = validate_cal_block(image)
        stored = self.read_cal_checksum()
        if stored is None:
            problems.append("the stored checksum at $%04X could not be read"
                            % CAL_CHECKSUM_ADDR)
        else:
            computed = cal_checksum(image)
            if stored != computed:
                problems.append(
                    "CHECKSUM MISMATCH: $%04X holds $%02X, the block sums to "
                    "$%02X. At the next power-up the firmware will discard this "
                    "calibration and substitute the ROM defaults ($0241)."
                    % (CAL_CHECKSUM_ADDR, stored, computed))
        return image, problems


# --------------------------------------------------------------------------- #
# Range helpers
# --------------------------------------------------------------------------- #
def voltage_ranges(include_divider: bool = True) -> List[str]:
    """Return the voltage range names, optionally excluding the Divider ranges."""
    return [name for name, r in RANGES.items()
            if r["kind"] == "V" and (include_divider or not r["divider"])]


def current_ranges() -> List[str]:
    """Return the current range names."""
    return [name for name, r in RANGES.items() if r["kind"] == "I"]


def to_entry_units(range_name: str, value_si: float) -> float:
    """Convert an SI measurement to the range's panel unit, ready for ``RD``."""
    return value_si / RANGES[range_name]["si"]


def describe_error(code: str) -> str:
    """Return the text for a displayed ``Err.Gnn`` code.

    Consults the codes printed in the Operation Manual first, then the two
    recovered from firmware that the manual omits, so that a G14 or a G19
    reaching an operator is explained rather than reported as unknown.
    """
    key = code.upper().replace("ERR.", "").strip()
    # Labels are not all upper case: the address error displays as GAd and the
    # BCD group as b01 to b04, so match case-insensitively.
    for known, text in ALL_GPIB_ERRORS.items():
        if known.upper() == key:
            return text
    return "Unknown error code."


def dac_connection_coefficient(a: float, b: float, d: float) -> float:
    """Return the coefficient the firmware stores from the three RKT entries.

    ``a``, ``b`` and ``d`` are the values entered at RKTA, RKTB and RKTD, in
    volts. The firmware computes ``(|b - a| / 10000) / |d - b|`` and stores the
    result scaled by 10^6, rounded. Confirmed against four deliberately chosen
    entry sets on 27 July 2026, which produced 10000, 8000, 10500 and 9979.

    Note that the coefficient rounds while the two limit coefficients in the
    same array truncate; the paths are separate in firmware.

    See :func:`dac_connection_stored` for the exact integer form the firmware
    actually evaluates; this float version is the readable approximation.
    """
    return 100.0 * (b - a) / (d - b)


def dac_connection_stored(a_mantissa: int, b_mantissa: int,
                          d_mantissa: int) -> int:
    """Return the exact value the firmware stores at $DD7D.

    The three arguments are the seven-digit mantissas as they sit in the entry
    buffers at $C022, $C02A and $C032 -- plain little-endian integers, upper
    four bytes zero, no exponent field. Entering 0.010000 leaves 10000.

    The firmware never works in decimal. Stage one at $4302 leaves
    ``|b - a| * 10**4`` at $C0A5; stage three at $435F divides that by
    ``|d - b|``, adds 50 and drops the low BCD byte::

        C0A5   = |b - a| * 10**4
        stored = (C0A5 // |d - b| + 50) // 100

    Verified against live RAM on 28 July 2026: after the 8000 set the buffers
    read 10000 / 110000 / 111250 and $C0A5 read 1_000_000_000 exactly.

    $C0A5 is referenced twice in the whole of ROM -- written at $4335, read at
    $4365 -- and is battery-backed, so it survives a power cycle.
    """
    numerator = abs(b_mantissa - a_mantissa) * 10 ** 4
    divisor = abs(d_mantissa - b_mantissa)
    if divisor == 0:
        raise ZeroDivisionError("RKTD equals RKTB; the firmware would divide "
                                "by zero")
    return (numerator // divisor + 50) // 100


#: Diagnostic addresses for watching a DAC connection adjustment. Sampling only
#: the stored coefficient cannot distinguish a recomputation from a stale
#: operand set; these are what a session needs to record instead.
CAL_SEQUENCE_FLAGS = {
    0xDE28: "RKTD-complete gate; set at $43D2, cleared at $0C9F",
    0xDE45: "computation latch; set at $0C69, cleared at $3570 and $3627",
    0xDE34: "recompute-and-commit-to-EEPROM; set only at $3577 (panel key)",
    0xDE35: "recompute-only; set at $362E (panel key)",
    0xDE5A: "cached EXT CAL switch, read by the gate at $93B0",
    0xDDF3: "program run in progress; read by the G20 gate at $93B5",
}

#: Entry buffers, 8 bytes each, little-endian integer mantissas.
CAL_ENTRY_BUFFERS = {
    0xC012: "zero point",
    0xC01A: "full-scale / range point",
    0xC022: "RKTA",
    0xC02A: "RKTB",
    0xC032: "RKTD",
    0xC0A5: "|B-A| * 10**4, stage-one intermediate",
}

#: The DAC connection coefficient has its own commit path, separate from REND.
#: When $DE34 is set the branch at $0C85 copies $DD7D -> $E154 (8 bytes, via
#: $40A5) and recomputes the master checksum at $402E. $DE34 is set at exactly
#: one site, $3577, inside a front-panel key handler; every other write stores
#: zero. So a panel adjustment commits immediately and a power cycle will not
#: undo it -- but no bus command can reach this path, and for a program REND
#: remains the only commit.
DAC_COEFF_PANEL_COMMIT = True
