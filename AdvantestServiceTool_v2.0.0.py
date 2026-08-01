"""Service, Verification and Adjustment Tool for calibratable instruments.

A menu-driven tool for servicing whichever instrument is plugged into it.
An instrument is a folder under ``instruments/``; see :mod:`instruments`.

Originally written for the Advantest R6161 DC Voltage/Current
Generator over GPIB, using a reference multimeter as the measurement standard.

    * Performance verification against the published accuracy specification,
      tagged as-found or as-left.
    * Full adjustment -- the complete 45-point Operation Manual 7.2.2 routine,
      automated end to end.
    * Calibration certificates -- ISO/IEC 17025-style PDFs generated on demand
      from saved results, in the operator's choice of language, as a single
      as-found or as-left report or as a combined report showing both.
    * Calibration-memory inspection: read and decode the 364-byte block, and
      compare two images to see exactly what an adjustment changed.

The tool is organised as a small set of single-responsibility classes wired
together by :class:`ServiceTool` (the menu controller):

    :class:`Console`            all terminal input and output.
    :class:`ReportConfig`       report_config.ini values and the output folder.
    :class:`CertificateWriter`  assemble certificate data and emit the PDFs.
    :class:`DmmManager`         bus scan, meter discovery and selection.
    :class:`Maintenance`        identity, bus scan, calibration-memory images.
    :class:`CalibrationEngine`  verification and adjustment, delegating the
                                procedures to :mod:`perf_engine` and
                                :mod:`adjust_engine`.
    :class:`ServiceTool`        connects to the generator and runs the menu.

Safety
------
The instrument is never written to without a deliberate confirmation, and the
adjustment routine commits nothing until the operator has reviewed the complete
table of values. ``Z`` and ``*RST`` are refused by the driver because they
discard the calibration data.

The EXT CAL switch is physical, on the rear panel. No command can move it, so
the tool prompts and verifies rather than assuming.

Requirements
------------
Python 3.8+, PyVISA, a VISA runtime with a GPIB interface, and reportlab for the
certificates.
"""

from __future__ import annotations

import os
import re
import sys
import time
import json
import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import i18n
import standards
from i18n import t

# Optional imports are checked at the point of use rather than at import time,
# so the tool still starts (and can, for example, generate a report) when a
# dependency for some other function is missing.
# The instrument is a plugin, chosen at startup and bound into :mod:`active`.
# Nothing here names one: ``active.driver`` is whichever driver module was
# loaded, and the timing settings below are assigned into that.
import active
import instruments
import units

try:
    import r6161_spec
except Exception:                                        # pragma: no cover
    r6161_spec = None

try:
    import dmm_driver
except Exception:                                        # pragma: no cover
    dmm_driver = None

try:
    import cal_report
except Exception:                                        # pragma: no cover
    cal_report = None

try:
    import uncertainty
except Exception:                                        # pragma: no cover
    uncertainty = None

try:
    import perf_engine
except Exception:                                        # pragma: no cover
    perf_engine = None

try:
    import adjust_engine
except Exception:                                        # pragma: no cover
    adjust_engine = None


VERSION = "2.0.0"

# =========================================================================== #
#                              U S E R   S E T T I N G S                      #
#                                                                             #
#  Edit these to suit the bench. They are applied to the measurement engines   #
#  at startup, so nothing else needs changing.                                 #
# =========================================================================== #

#: Interface language. Edit to any code present in ``lang/ui/``; it can also be
#: changed at run time from the menu.
UI_LANGUAGE = "en"

#: Default VISA resource for the generator. Overridable at startup.
DEFAULT_RESOURCE = "GPIB0::4::INSTR"
#: Overwritten at startup by the chosen instrument's manifest, so
#: the value above is only the fallback for a bench that has none.

#: Seconds to wait after OPERATE before a point is read, for every range except
#: 1000 V. Manual 7.2.2 pre-procedure note 8 asks for approximately five; the
#: module default matches that, and this is deliberately longer.
#:
#: Measured on this instrument on 28 July 2026, stepping from STANDBY to full
#: scale and watching the output on a 3458A at NPLC 1 (three repeats per range,
#: raw data and curves in ``settling_*.json`` / ``settling_*.html``):
#:
#:     range    into 32 ppm   into 10 ppm   error at 5 s   error at 10 s
#:      1 V        0.5 s         0.7 s        -2 .. 0 ppm       -
#:     10 V        0.5 s         0.7 s          -0.8 ppm        -
#:    100 V        0.9 s         1.5 s     -1.6 .. -2.9 ppm     -
#:
#: Five seconds was already sound; ten is taken because the residual error is
#: **systematically negative** on every range -- the output always approaches
#: from below and never overshoots -- so it is a bias that repeats cannot
#: average away, and during an adjustment it is written into the coefficient.
#: The cost is 45 points x 5 s, under four minutes on a full run.
SETTLE_S = 10.0

#: Seconds to wait after OPERATE on the 1000 V range.
#:
#: **This is the manual's own figure**, page 7-15, footnote *1 against the
#: 1000 V range's steps: "Use the data displayed approximately 20 seconds after
#: the OPERATE operation." The five-second note on page 7-5 explicitly excepts
#: this range, and this is where the exception is answered.
#:
#: This range really is much slower than the others: 2.4 s to reach 32 ppm,
#: 4.9 s to reach 10 ppm, and about 9 s to reach 3 ppm. At 20 s the residual is
#: below the 2.1 ppm of run-to-run drift from the output stage warming under
#: repeated 1000 V loading, which is the floor worth chasing to.
SETTLE_HV_S = 20.0

#: Seconds to wait before reading a point on the **100 mA range**.
#:
#: The manual's own figure again, and the odd one out. Pages 7-27 and 7-28 --
#: and only those two, which are the 100 mA range's pages -- carry the line
#: "The data about 60 seconds after the operation is follows". Six times the
#: general figure, with no reason given.
#:
#: The likely reason is heat. 100 mA through the 10 ohm standard the same page
#: specifies is 0.1 W, where every other point in the whole procedure dissipates
#: a thousandth of that, and a resistor's value moves as it warms. Whatever the
#: cause, this range is worth the extra minute: it adds four minutes to a run
#: and the manufacturer asked for it.
#:
#: Unlike the two figures above, this one has NOT been checked by measurement
#: here -- the settling study covered the voltage ranges only.
SETTLE_100MA_S = 60.0

#: Seconds allowed for thermoelectric EMF to settle after a hookup change, on
#: the groups that need it. Only the divider group does at present: manual 7.2.2
#: directs that copper clips be used there and that one minute be allowed.
#:
#: At 10 mV full scale a 1 microvolt thermal offset is 100 ppm, which is larger
#: than the tolerance the reading is graded against, so this is worth
#: lengthening rather than shortening if the bench is draughty or the clips are
#: dissimilar metals. Set to 0 to skip the wait entirely.
THERMAL_WAIT_S = 60

#: Alert the operator when the tool is waiting for an answer.
#:
#: A run is mostly waiting -- ten seconds a point, a minute on the divider
#: hookup, forty minutes end to end -- so nobody sensibly watches it. The
#: console title bar flashes and the system alert sound plays when a prompt
#: appears, which is what brings them back to the bench.
#:
#: Set to False for an unattended or scripted run, where the beep would be to
#: an empty room.
ALERT_ON_PROMPT = True

#: Seconds of quiet before another alert is allowed.
#:
#: Prompts arrive in bursts -- the reference-standard details alone are three
#: questions per standard -- and an alert on each would be noise that teaches
#: the operator to ignore it. One alert brings them to the keyboard; the rest of
#: the burst is answered while they are already there.
ALERT_MIN_GAP_S = 20.0

# The reference multimeter's integration time and displayed resolution are NOT
# set here. They are properties of the meter rather than of the run, and they
# live in that meter's own file in `dmm_configs/` as the `nplc` and `ndig` keys
# -- for the 3458A, 100 PLC and NDIG 8. Putting them here would have meant one
# number applied to eleven meters with different capabilities.

# =========================================================================== #
#                        end of user settings                                 #
# =========================================================================== #

#: All file paths resolve relative to this script, never the working directory,
#: so the folder stays portable however the tool is launched.
HERE = os.path.dirname(os.path.abspath(__file__))

#: Human-readable names for the saved-run tags, used in the report menu.
TAG_LABELS: Dict[str, str] = {
    "verify": "Performance Verification",
    "adjust": "Full Adjustment",
}

#: The same tags for a table column, where the full names would set the width of
#: every other column. Older saved runs may carry ``adjust``; adjustments no
#: longer save a run, but files from before that change still list.
TAG_SHORT: Dict[str, str] = {
    "verify": "Verification",
    "adjust": "Adjustment",
}

#: How a saved run's aspect is written for a human. The stored values are
#: single words so that they are safe in a filename and a JSON key; a list the
#: operator chooses from should read the way the certificate does.
ASPECT_LABELS: Dict[str, str] = {
    "asfound": "as-found",
    "asleft": "as-left",
    "adjust": "adjustment",
}


#: The values :mod:`r6161` ships with, captured before anything is assigned over
#: them so that "default" in a message stays true however often
#: :func:`apply_user_settings` is called.
_SETTING_NAMES = ("SETTLE_S", "SETTLE_HV_S", "SETTLE_100MA_S",
                  "THERMAL_WAIT_S")

#: Filled in by :func:`capture_timing_defaults` once an instrument is bound,
#: because "the default" is a property of the instrument and there is no
#: instrument at import time.
_TIMING_DEFAULTS: Dict[str, float] = {}


def capture_timing_defaults() -> None:
    """Record the bound instrument's own timing values as the defaults.

    Called immediately after binding and before any setting is applied over
    them, so that "keeping the default" in a later message names the number the
    instrument shipped with rather than whatever was last assigned.
    """
    _TIMING_DEFAULTS.clear()
    for name in _SETTING_NAMES:
        if hasattr(active.driver, name):
            _TIMING_DEFAULTS[name] = getattr(active.driver, name)


def apply_user_settings() -> List[str]:
    """Push the timing settings into :mod:`r6161` and report anything wrong.

    The engines read ``r6161.SETTLE_S``, ``r6161.SETTLE_HV_S``,
    ``r6161.SETTLE_100MA_S`` and ``r6161.THERMAL_WAIT_S`` at the moment they
    need them, so assigning here is enough and no other module has to be edited.

    Only **problems** are returned: a setting that could not be used, or one
    whose value has a consequence worth naming. A setting that simply differs
    from the module default is not news at startup -- all four are shown under
    *Instrument information*, where they can be read at leisure rather than
    scrolling past on every launch.

    A setting that cannot be used is reported and the module default is kept:
    silently substituting a value for a run that produces a calibration
    certificate would put an unrecorded change into a traceable document.
    """
    notes: List[str] = []
    if not active.is_bound():
        return ["no instrument is bound; timing settings not applied"]

    for name, value, floor in (("SETTLE_S", SETTLE_S, 0.0),
                               ("SETTLE_HV_S", SETTLE_HV_S, 0.0),
                               ("SETTLE_100MA_S", SETTLE_100MA_S, 0.0),
                               ("THERMAL_WAIT_S", THERMAL_WAIT_S, 0.0)):
        default = _TIMING_DEFAULTS.get(name)
        if default is None:
            # A setting this instrument does not have. Not an
            # error: another instrument may have no divider and
            # so no thermal wait.
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            notes.append("%s is not a number; keeping %g" % (name, default))
            continue
        if number < floor:
            notes.append("%s must be at least %g; keeping %g"
                         % (name, floor, default))
            continue
        setattr(active.driver, name, number)

    if active.driver.SETTLE_S == 0 or active.driver.SETTLE_HV_S == 0:
        notes.append("a settling time of zero will be accepted by the "
                     "instrument but the reading may be taken before the "
                     "output has arrived")
    if active.driver.THERMAL_WAIT_S == 0:
        notes.append("the thermal wait is disabled; divider-range readings "
                     "may carry a thermoelectric offset")
    return notes


def report_data_dir() -> str:
    """Return the folder holding saved run results, creating it if needed."""
    path = os.path.join(HERE, "report_data")
    os.makedirs(path, exist_ok=True)
    return path


def cal_images_dir() -> str:
    """Return the folder holding calibration-memory images, creating it."""
    path = os.path.join(HERE, "cal_images")
    os.makedirs(path, exist_ok=True)
    return path


# --------------------------------------------------------------------------- #
# Console
# --------------------------------------------------------------------------- #
class Console:
    """All terminal input and output.

    Holds no state; it exists as a class so it can be passed to collaborators as
    one object rather than imported as a module of loose functions.
    """

    WIDTH = 74

    #: When the last alert was raised, so a burst of prompts makes one noise.
    _last_alert = [0.0]

    def hr(self) -> None:
        """Draw a horizontal rule."""
        print("-" * self.WIDTH)

    @classmethod
    def alert(cls) -> None:
        """Flash the console window and sound the system alert.

        Called when the tool starts waiting for an answer. A run is mostly
        waiting, so the operator is rarely watching; this is what fetches them.

        Throttled by ``ALERT_MIN_GAP_S``: prompts arrive in bursts, and one
        alert per question would be noise the operator learns to ignore.

        Everything here is best-effort. A bell that cannot be rung must not stop
        a calibration, so every step is guarded and a platform without a console
        window simply gets the terminal bell.
        """
        if not ALERT_ON_PROMPT:
            return
        now = time.time()
        if now - cls._last_alert[0] < ALERT_MIN_GAP_S:
            return
        cls._last_alert[0] = now
        try:
            import ctypes
            # MB_ICONEXCLAMATION; the OS sound scheme decides what it sounds
            # like, which is the point -- it matches whatever else on this
            # machine asks for attention.
            ctypes.windll.user32.MessageBeep(0x00000030)
            handle = ctypes.windll.kernel32.GetConsoleWindow()
            if handle:
                class FLASHWINFO(ctypes.Structure):
                    _fields_ = [("cbSize", ctypes.c_uint),
                                ("hwnd", ctypes.c_void_p),
                                ("dwFlags", ctypes.c_uint),
                                ("uCount", ctypes.c_uint),
                                ("dwTimeout", ctypes.c_uint)]
                # FLASHW_ALL | FLASHW_TIMERNOFG: flash caption and taskbar
                # button until the window comes to the foreground.
                info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), handle,
                                  0x00000003 | 0x0000000C, 0, 0)
                ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
                return
        except Exception:
            pass
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass

    def pause(self) -> None:
        """Wait for the operator before redrawing the menu."""
        self.alert()
        try:
            input(t("\nPress Enter to continue ... "))
        except EOFError:
            pass

    def ask(self, prompt: str, default: Optional[str] = None) -> str:
        """Prompt for a string, returning ``default`` for an empty answer."""
        self.alert()
        try:
            answer = input(prompt).strip()
        except EOFError:
            return default or ""
        return answer if answer else (default or "")

    def ask_float(self, prompt: str, default: Optional[float] = None) -> float:
        """Prompt for a number, re-asking until one parses."""
        while True:
            answer = self.ask(prompt)
            if not answer and default is not None:
                return float(default)
            try:
                return float(answer)
            except ValueError:
                print(t("  Not a number; try again."))

    def yesno(self, prompt: str, default: bool = False) -> bool:
        """Prompt for yes or no. Destructive actions should default to ``False``."""
        suffix = " [Y/n]: " if default else " [y/N]: "
        answer = self.ask(prompt + suffix).strip().lower()
        if not answer:
            return default
        return answer.startswith("y")

    def choose(self, prompt: str, options: List[str],
               default_index: int = 0) -> Optional[int]:
        """Present a numbered list and return the chosen index, or ``None``.

        ``0`` always means cancel, at every level of the interface.
        """
        for index, label in enumerate(options, start=1):
            marker = "  [default]" if (index - 1) == default_index else ""
            print("  %2d) %s%s" % (index, label, marker))
        print(t("   0) Cancel"))
        answer = self.ask(prompt, default=str(default_index + 1))
        if answer.strip() == "0":
            return None
        try:
            index = int(answer) - 1
        except ValueError:
            return None
        return index if 0 <= index < len(options) else None

    def choose_numbers(self, prompt: str, count: int, default: str = "",
                       limit: int = 2,
                       minimum: int = 1) -> Optional[List[int]]:
        """Read one or several list numbers and return them as indices.

        Unlike :meth:`choose`, a nonsensical answer is not silently taken for a
        cancellation: it says what was wrong and asks again. Picking the wrong
        run produces a certificate for the wrong measurements, so a typo here
        must not pass quietly.

        ``0`` cancels, as everywhere else. Numbers may be separated by anything
        reasonable -- ``1,3``, ``1 3``, ``1-3``, ``1 and 3`` -- because an
        operator told to "enter two numbers" will use whichever they think of.
        """
        while True:
            answer = self.ask(prompt, default=default).strip().lower()
            if not answer:
                return None
            tokens = [token for token in re.split(r"[^0-9]+", answer) if token]
            if "0" in tokens:
                return None
            if not tokens:
                print(t("  Enter a number from the list, or 0 to cancel."))
                continue
            numbers = [int(token) for token in tokens]
            if len(numbers) > limit:
                print(t("  That is %d numbers; at most %d can be used.")
                      % (len(numbers), limit))
                continue
            if len(numbers) < minimum:
                print(t("  That is %d number; %d are needed.")
                      % (len(numbers), minimum))
                continue
            out_of_range = [n for n in numbers if not 1 <= n <= count]
            if out_of_range:
                print(t("  No run number %s in the list -- pick from 1 to %d.")
                      % (", ".join(str(n) for n in out_of_range), count))
                continue
            if len(set(numbers)) != len(numbers):
                print(t("  That is the same run twice; pick two different runs."))
                continue
            return [n - 1 for n in numbers]

    @staticmethod
    def table(header: Sequence[str], rows: Sequence[Sequence[str]],
              right: Sequence[int] = ()) -> List[str]:
        """Lay out a table as text, sizing every column to its widest cell.

        Returned rather than printed so a caller can put it in a prompt, a log
        or a test comparison. Column widths are computed rather than fixed
        because the widest serial number or certificate number is not known
        until the files are read.
        """
        cells = [list(header)] + [list(row) for row in rows]
        widths = [max(len(str(row[col])) for row in cells)
                  for col in range(len(header))]
        lines = []
        for index, row in enumerate(cells):
            parts = []
            for col, value in enumerate(row):
                text = str(value)
                parts.append(text.rjust(widths[col]) if col in right
                             else text.ljust(widths[col]))
            lines.append(("   " + "  ".join(parts)).rstrip())
            if index == 0:
                lines.append("   " + "  ".join("-" * width for width in widths))
        return lines

    def section_choice(self, name: str) -> str:
        """Offer run/skip/exit for one section of a procedure."""
        print("")
        answer = self.ask(
            t("%s -- [R]un / [S]kip / e[X]it to menu [R]: ") % name,
            default="R").strip().lower()
        if answer.startswith("x"):
            return "exit"
        if answer.startswith("s"):
            return "skip"
        return "run"

    @staticmethod
    def progress_bar(done: int, total: int, width: int = 40,
                     label: str = "") -> None:
        """Draw an in-place progress bar; a newline is emitted when complete."""
        if not total:
            return
        filled = int(width * done / total)
        sys.stdout.write("\r  %-10s [%s%s] %d/%d"
                         % (label, "#" * filled, "." * (width - filled),
                            done, total))
        sys.stdout.flush()
        if done >= total:
            sys.stdout.write("\n")


# --------------------------------------------------------------------------- #
# Report configuration
# --------------------------------------------------------------------------- #
def _default_cert_prefix() -> str:
    """Certificate prefix for the bound instrument, when the ini is silent."""
    if active.is_bound():
        return "%s-CAL" % active.instrument.plugin.key.upper()
    return "CAL"


class ReportConfig:
    """Values from ``report_config.ini`` that appear on a certificate.

    Reloaded on every pass of the menu loop rather than cached, so an operator
    can edit the file mid-session -- to correct a customer name, say -- and have
    the change apply to the next action without restarting.
    """

    def __init__(self, lab: Dict[str, str], customer: Dict[str, str],
                 operator: str, approver: str, temperature_c: str,
                 humidity_pct: str, output_dir: str, cert_prefix: str,
                 languages: List[str], interval: str,
                 uut: Optional[Dict[str, str]] = None) -> None:
        self.lab = lab
        self.customer = customer
        #: Serial number and options of the instrument being calibrated. Held
        #: here only as the default the run's prompt offers -- a bench that
        #: calibrates the same R6161 repeatedly can fill them in once and press
        #: Enter thereafter.
        self.uut = dict(uut or {"serial": "", "options": ""})
        self.operator = operator
        self.approver = approver
        self.temperature_c = temperature_c
        self.humidity_pct = humidity_pct
        self.output_dir = output_dir
        self.cert_prefix = cert_prefix
        self.languages = languages
        self.interval = interval

    @classmethod
    def load(cls) -> "ReportConfig":
        """Parse ``report_config.ini``, falling back to blank values."""
        import configparser
        parser = configparser.ConfigParser()
        path = os.path.join(HERE, "report_config.ini")
        try:
            parser.read(path, encoding="utf-8")
        except Exception:
            pass

        def get(section: str, key: str, default: str = "") -> str:
            try:
                value = parser.get(section, key)
            except Exception:
                return default
            return value.strip() if value and value.strip() else default

        languages = [code.strip() for code
                     in get("report", "languages", "en").split(",")
                     if code.strip()]
        interval = get("report", "interval",
                       r6161_spec.DEFAULT_INTERVAL if r6161_spec else "90day")
        return cls(
            lab=dict(name=get("laboratory", "name"),
                     address=get("laboratory", "address"),
                     accreditation=get("laboratory", "accreditation")),
            customer=dict(name=get("customer", "name"),
                          address=get("customer", "address")),
            operator=get("personnel", "operator"),
            approver=get("personnel", "approver"),
            temperature_c=get("conditions", "temperature_c", "23.0"),
            humidity_pct=get("conditions", "humidity_pct", "45"),
            output_dir=get("report", "output_dir", "reports"),
            cert_prefix=get("report", "cert_prefix", _default_cert_prefix()),
            languages=languages or ["en"],
            interval=interval,
            uut=dict(serial=get("uut", "serial"),
                     options=get("uut", "options")))

    def reports_dir(self) -> str:
        """Return the certificate output folder, creating it if needed."""
        path = self.output_dir
        if not os.path.isabs(path):
            path = os.path.join(HERE, path)
        os.makedirs(path, exist_ok=True)
        return path


# --------------------------------------------------------------------------- #
# Certificate assembly and persistence
# --------------------------------------------------------------------------- #
#: What the certificate says about measurement uncertainty.
#:
#: What the certificate says about measurement uncertainty when NOTHING has been
#: evaluated -- the state of a bench that has not written ``uncertainty.ini``.
#:
#: The generator's default statement describes a k = 2 expanded uncertainty
#: derived from the standards used. With no budget the uncertainty column is
#: empty, so printing that statement would describe a budget the document does
#: not contain; the certificate should say so plainly rather than imply
#: otherwise. Where a budget HAS been written, the run replaces this with
#: :meth:`uncertainty.Budget.note`, which describes what was actually applied --
#: including which decision rule graded the rows.
UNCERTAINTY_NOTE = (uncertainty.Budget().note() if uncertainty is not None else
                    "No measurement uncertainty budget is reported.")


class CertificateWriter:
    """Assemble certificate data, persist runs, and emit the PDFs.

    Persistence is deliberately flat: one JSON file per (test type, session),
    holding the complete dict the renderer consumes. All of the intelligence
    about pairing an as-found run with an as-left run lives in
    :class:`ServiceTool` at report time, not here.
    """

    def __init__(self, gen, session_id: str) -> None:
        self.gen = gen
        self.session_id = session_id

    def build(self, rc: ReportConfig, test_type_key: str, results: List[dict],
              procedure: str, standards: List[dict],
              section_labels: Optional[Dict[str, str]] = None,
              spec_note: str = "") -> Tuple[Dict[str, Any], str]:
        """Assemble a certificate dict and allocate a certificate number.

        The shape is the certificate generator's data contract -- nested
        ``lab`` / ``customer`` / ``uut`` / ``conditions`` blocks, a list of
        reference standards, and rows built by ``cal_report.make_row`` so that
        every verdict is derived from the numbers rather than asserted.
        """
        now = datetime.datetime.now()
        cert_no = "%s-%s" % (rc.cert_prefix, now.strftime("%Y%m%d-%H%M%S"))
        serial = getattr(self.gen, "serial", "") or ""
        data: Dict[str, Any] = {
            "cert_number": cert_no,
            "cal_date": now.strftime("%Y-%m-%d"),
            "issue_date": now.strftime("%Y-%m-%d"),
            "test_type_key": test_type_key,
            "lab": {
                "name": rc.lab["name"],
                "address": rc.lab["address"],
                "accreditation": rc.lab["accreditation"],
            },
            "customer": {
                "name": rc.customer["name"],
                "address": rc.customer["address"],
            },
            # Everything ``*IDN?`` carries is taken from the instrument, so the
            # certificate describes what was actually on the bench. Only the
            # serial number and the option list are typed, because the R6161
            # does not report them; both are asked at the start of the run.
            "uut": {
                "description": getattr(self.gen, "description", "")
                               or "DC Voltage/Current Source",
                "model": getattr(self.gen, "model", "") or (
                    active.instrument.name if active.is_bound() else "-"),
                "serial": serial or "-",
                "firmware": getattr(self.gen, "firmware", "") or "-",
                "options": (getattr(self.gen, "options", "") or "").strip() or "-",
            },
            "conditions": {
                "temperature_c": rc.temperature_c,
                "humidity_pct": rc.humidity_pct,
                "procedure": procedure,
            },
            "standards": standards,
            "results": results,
            "section_labels": dict(section_labels or {}),
            "operator": rc.operator,
            "approver": rc.approver,
        }
        if spec_note:
            data["spec_note"] = spec_note
        # This tool measures no uncertainty budget, so the generator's default
        # statement -- which describes one -- would be a claim the certificate
        # does not support. Say what is true instead.
        data["strings"] = {"unc_body": UNCERTAINTY_NOTE}
        return data, cert_no

    def save(self, data: Dict[str, Any], cert_no: str, tag: str) -> str:
        """Write one run to ``report_data/`` and return the path.

        The filename is ``<serial>_<tag>_<session>.json``. Re-running the same
        test type within one launch therefore overwrites that session's file,
        while each new launch produces a new one -- a history of sessions rather
        than of individual attempts.
        """
        uut = data.get("uut", {})
        serial = (uut.get("serial") or "unknown").replace(os.sep, "_")
        payload = {
            "saved_at": datetime.datetime.now().isoformat(),
            "session_id": self.session_id,
            "tag": tag,
            "serial": serial,
            "model": uut.get("model", ""),
            "test_type_key": data.get("test_type_key"),
            "cert_number": cert_no,
            "cal_date": data.get("cal_date"),
            "data": data,
        }
        name = "%s_%s_%s.json" % (serial, tag, self.session_id)
        path = os.path.join(report_data_dir(), name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1, ensure_ascii=False)
        return path

    @staticmethod
    def list_saved() -> List[dict]:
        """Return every saved run, newest first.

        Unparseable files are skipped rather than reported: a corrupt file
        should not prevent the operator from certifying the good runs beside it.
        """
        runs: List[dict] = []
        folder = report_data_dir()
        for name in os.listdir(folder):
            if not name.lower().endswith(".json"):
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    payload = json.load(handle)
            except Exception:
                continue
            payload["_path"] = path
            try:
                payload["_mtime"] = os.path.getmtime(path)
            except Exception:
                payload["_mtime"] = 0
            runs.append(payload)
        runs.sort(key=lambda r: (r.get("saved_at", ""), r.get("_mtime", 0)),
                  reverse=True)
        return runs

    def generate_pdfs(self, rc: ReportConfig, data: Dict[str, Any],
                      basename: str, languages: List[str]) -> List[str]:
        """Render the certificate in each requested language."""
        if cal_report is None:
            print(t("reportlab is not installed; cannot generate certificates."))
            return []
        return cal_report.generate_all(data, rc.reports_dir(), basename, languages)


# --------------------------------------------------------------------------- #
# Reference multimeter management
# --------------------------------------------------------------------------- #
class DmmManager:
    """Bus scan, meter discovery, selection and the reference-standard row."""

    #: Config ``[identify] match`` of a meter worth using on the Divider ranges
    #: alone. The 34420A is a nanovolt meter: inadequate at 10 V, and the best
    #: available match at 10 mV, where the R6161's own specification is loosest
    #: (290 ppm) and an ordinary 8.5-digit meter is working near its floor.
    NANOVOLT_MATCH = "34420A"

    #: Plan groups the nanovolt meter is offered for. Both perf_plan and
    #: adjust_plan name the Divider group "divider"; the current groups are
    #: excluded because the 34420A has no DC current function at all.
    DIVIDER_GROUP_KEYS = ("divider",)

    def __init__(self, gen, console: Console) -> None:
        self.gen = gen
        self.console = console
        self.bus_scan: List[Tuple[str, Optional[str]]] = []
        self.chosen_config: Optional[str] = None
        self.chosen_resource: Optional[str] = None
        self.last_used: Optional[Any] = None
        # A second meter, used only for the Divider groups. None means one
        # meter does everything, which is the ordinary case.
        self.divider_config: Optional[str] = None
        self.divider_resource: Optional[str] = None
        #: Every meter opened this session, so a certificate can name them all.
        self.used: List[Any] = []
        #: Open sessions, keyed by config path. A procedure spans several hookup
        #: groups and used to open and close the meter around each one, which
        #: cost around fifteen seconds a group: the restore block leaves a 3458A
        #: free-running, so the drain that follows reads its full forty-reading
        #: limit instead of timing out, and the reconnect then drains twenty
        #: more. Nothing about that was measuring anything. The session is now
        #: held open for the length of a run and released by close_all().
        self._sessions: Dict[str, Any] = {}

    def scan(self, announce: bool = True) -> List[Tuple[str, Optional[str]]]:
        """Enumerate the bus, skipping the generator's own address.

        The generator is skipped deliberately: probing it with a write would
        corrupt whatever command follows, and it has already been identified.
        """
        if dmm_driver is None:
            return []
        if announce:
            print(t("Scanning the GPIB bus ..."))
        own = getattr(self.gen, "resource", "")
        self.bus_scan = dmm_driver.scan_bus(skip=[own] if own else [])
        if announce:
            for resource, identity in self.bus_scan:
                print("   %-24s %s" % (resource, identity or t("(no response)")))
        return self.bus_scan

    # ---- two-meter working ---------------------------------------------- #
    def nanovolt_candidate(self) -> Optional[str]:
        """Return the nanovolt meter's config path if one is on the bus.

        Only when there is *another* usable meter as well. On its own the
        34420A cannot do a run at all -- no DC current function and a 100 V
        ceiling -- so offering to split the work would be offering nothing.
        """
        if dmm_driver is None or not self.bus_scan:
            return None
        nano = None
        others = 0
        for path in dmm_driver.list_configs():
            if not dmm_driver.match_config(path, self.bus_scan):
                continue
            if self.NANOVOLT_MATCH in os.path.basename(path).upper():
                nano = path
            elif dmm_driver.DMMConfig(path).has_function("dcv"):
                others += 1
        return nano if (nano and others) else None

    def offer_nanovolt_meter(self) -> None:
        """Ask, once per run, whether to use the nanovolt meter on Divider.

        Asked at the start of a procedure rather than when the Divider group is
        reached, because it changes what the operator has to cable up and they
        should know before they begin, not four hookups in.
        """
        path = self.nanovolt_candidate()
        if path is None:
            self.divider_config = self.divider_resource = None
            return

        cfg = dmm_driver.DMMConfig(path)
        hit = dmm_driver.match_config(path, self.bus_scan)
        # The case for splitting the work -- that this is a nanovolt meter,
        # inadequate at 10 V and the best thing available at 10 mV, where the
        # R6161's own specification is loosest -- is in the README under "Two
        # meters, one run". It does not belong in front of an operator who has
        # already decided, and who sees this prompt at the start of every run.
        print("")
        print(t("A %s is connected via GPIB.") % cfg.model)
        self.console.hr()
        if self.console.yesno(
                t("Use the %s for the Divider ranges?") % cfg.model,
                default=False):
            self.divider_config = path
            self.divider_resource = hit[0] if hit else None
            print(t("   Divider ranges: %s") % cfg.name)
        else:
            self.divider_config = self.divider_resource = None
            print(t("   One meter for everything."))

    def _wants_nanovolt(self, group: Optional[dict], function: str) -> bool:
        """Is this the group the second meter was chosen for?"""
        if not self.divider_config or group is None:
            return False
        if group.get("key") not in self.DIVIDER_GROUP_KEYS:
            return False
        return dmm_driver.DMMConfig(self.divider_config).has_function(function)

    def select(self, function: str = "dcv", group: Optional[dict] = None):
        """Return a connected meter that can perform ``function``.

        The choice is remembered for the rest of the session, so a procedure
        that spans several hookup groups does not re-ask between them.

        ``group`` is the plan group about to be measured. It is what allows a
        second meter to be used for the Divider ranges only; pass it and the
        right instrument is chosen without anybody being asked again.
        """
        if self._wants_nanovolt(group, function):
            meter = self._open(self.divider_config, self.divider_resource,
                               function)
            if meter is not None:
                return meter
            print(t("   the Divider meter would not open; using the main one."))

        if dmm_driver is None:
            print(t("dmm_driver is unavailable; falling back to manual entry."))
            self.last_used = dmm_driver_manual(self.console)
            return self.last_used

        if self.chosen_config:
            meter = self._open(self.chosen_config, self.chosen_resource, function)
            if meter is not None:
                return meter
            print(t("The previously chosen meter could not be reopened."))
            self.chosen_config = None

        configs = dmm_driver.list_configs()
        if not configs:
            print(t("No meter configs found in dmm_configs/."))
            return self._manual()

        if not self.bus_scan:
            self.scan(announce=False)

        # If exactly one supplied config matches something that answered on the
        # bus, that is not a choice -- it is the answer. Asking anyway trains
        # the operator to press Enter without reading, which is how the wrong
        # meter eventually gets picked. Two or more matches IS a choice, and is
        # still put to them.
        usable = [path for path in configs
                  if dmm_driver.match_config(path, self.bus_scan)
                  and dmm_driver.DMMConfig(path).has_function(function)]
        if len(usable) == 1:
            path = usable[0]
            resource, identity = dmm_driver.match_config(path, self.bus_scan)
            # _open announces what it opened and records it as last_used.
            meter = self._open(path, resource, function)
            if meter is not None:
                self.chosen_config, self.chosen_resource = path, resource
                return meter
            print(t("   %s answered the scan but would not open; choose below.")
                  % dmm_driver.DMMConfig(path).name)

        labels: List[str] = []
        for path in configs:
            cfg = dmm_driver.DMMConfig(path)
            hit = dmm_driver.match_config(path, self.bus_scan)
            # The resolution class is worth showing: the R6161's own 90-day
            # specification is 25 ppm of setting on the 1 V and 10 V ranges, so
            # a 6.5 digit meter is marginal against it and a 7.5 or 8.5 digit
            # one is not.
            digits = cfg.get("dmm", "digits", "")
            digit_text = ("  [%s digit]" % digits) if digits else ""
            supports = "" if cfg.has_function(function) else t("  (no %s section)") % function
            seen = t("  <- found at %s") % hit[0] if hit else ""
            labels.append("%s%s%s%s" % (cfg.name, digit_text, seen, supports))
        labels.append(t("Manual entry (type the reading in)"))

        print("")
        print(t("Select the reference multimeter:"))
        # Pre-select a meter that was actually found on the bus.
        default_index = 0
        for index, path in enumerate(configs):
            if dmm_driver.match_config(path, self.bus_scan):
                default_index = index
                break
        index = self.console.choose(t("Meter: "), labels, default_index)
        if index is None:
            return None
        if index == len(configs):
            return self._manual()

        path = configs[index]
        hit = dmm_driver.match_config(path, self.bus_scan)
        resource = hit[0] if hit else None
        if resource is None:
            address = self.console.ask(
                t("GPIB address of this meter [22]: "), default="22")
            resource = None
            meter = self._open(path, None, function, address=address)
        else:
            meter = self._open(path, resource, function)
        if meter is None:
            return self._manual()
        self.chosen_config, self.chosen_resource = path, resource
        return meter

    def _open(self, config_path: str, resource: Optional[str], function: str,
              address: Optional[str] = None):
        """Return a live session for this meter, opening one if need be.

        An already-open session is handed back as it is. Reopening it would
        cost a device clear, a drain and an init block for no measurement, and
        it is the same instrument either way.
        """
        existing = self._sessions.get(config_path)
        if existing is not None and getattr(existing, "inst", None) is not None:
            if not existing.cfg.has_function(function):
                print(t("   WARNING: this config has no [%s] section.") % function)
            self.last_used = existing
            return existing
        try:
            meter = dmm_driver.DMM(config_path, resource=resource,
                                   address=address)
            opened = meter.connect()
            # Announce the meter only when there is a choice to be confused
            # about. With one meter it is the same line before every group,
            # saying what the operator already knows; with two it is the answer
            # to "which one is this group on?", and the address adds nothing to
            # that -- the name is what is written on the front panel.
            if self.divider_config:
                print(t("   meter: %s") % meter.cfg.name)
            if not meter.cfg.has_function(function):
                print(t("   WARNING: this config has no [%s] section.") % function)
            self.last_used = meter
            self._sessions[config_path] = meter
            # Remember one instance per distinct meter. A certificate has to
            # name every standard the measurements were actually made with, and
            # with two meters in play "the last one" is not that.
            if not any(getattr(m, "cfg", None) is not None
                       and m.cfg.path == meter.cfg.path for m in self.used):
                self.used.append(meter)
            return meter
        except Exception as exc:
            print(t("   could not open the meter: %s") % exc)
            return None

    def _manual(self):
        """Return a keyboard-entry stand-in for a meter."""
        self.last_used = dmm_driver.ManualDMM(self.console)
        print(t("   using manual entry; you will be asked for each reading."))
        return self.last_used

    # ---- opening and closing for a whole run ------------------------------ #
    def preopen(self, plan: List[dict]) -> None:
        """Open every meter a plan will need, before the first hookup.

        Opening early is worth two things. The operator is asked which meter to
        use -- if there is a choice to make -- before they start cabling rather
        than in the middle of it, and the certificate can list what the run is
        traceable to at the point where the answers are easy to give.

        A meter that will not open is not an error here: the group that needs it
        will say so when it is reached, and a run that skips that group never
        needed it.
        """
        for group in plan:
            try:
                self.select(function=group.get("dmm_function", "DCV").lower(),
                            group=group)
            except Exception:
                continue

    def close_all(self) -> None:
        """Restore and close every open session. Never raises."""
        for meter in list(self._sessions.values()):
            try:
                meter.restore()
            except Exception:
                pass
            try:
                meter.close()
            except Exception:
                pass
        self._sessions.clear()

    # ---- the certificate's reference-standard rows ------------------------ #
    def serial_for(self, meter) -> str:
        """The serial number the bus scan already reported for this meter.

        Only SCPI instruments have told us: a four-field ``*IDN?`` response
        carries the serial in its third field. The 3458A answers ``ID?`` with
        the word ``HP3458A`` and nothing else, so this returns ``""`` and the
        operator types it in.
        """
        try:
            wanted = meter.resource_string()
        except Exception:
            return ""
        for resource, identity in self.bus_scan:
            if resource == wanted:
                return standards.serial_from_identity(identity)
        return ""

    def standard_rows(self) -> List[dict]:
        """One certificate row per meter this session has actually opened."""
        meters = self.used or ([self.last_used] if self.last_used else [])
        rows: List[dict] = []
        for meter in meters:
            cfg = getattr(meter, "cfg", None)
            name = getattr(cfg, "name", None) or "Reference multimeter"
            model = getattr(cfg, "model", None) or "-"
            rows.append(standards.entry(
                "Reference multimeter (%s)" % name, model,
                serial=self.serial_for(meter) or "-"))
        return rows

    def standard_entry(self, meter) -> dict:
        """The certificate row for one meter, asked for on the spot.

        Kept for callers that have not collected their standards up front. The
        run paths do: they build every row with :meth:`standard_rows` and put
        the questions to the operator once, before the first hookup.
        """
        meter = meter or self.last_used
        cfg = getattr(meter, "cfg", None)
        row = standards.entry(
            "Reference multimeter (%s)"
            % (getattr(cfg, "name", None) or "Reference multimeter"),
            getattr(cfg, "model", None) or "-",
            serial=self.serial_for(meter) or "-")
        standards.collect(self.console, [row])
        return row


def dmm_driver_manual(console: Console):
    """Return a manual meter even when :mod:`dmm_driver` failed to import."""
    class _Fallback:
        manual = True

        class _Cfg:
            name = "Manual entry"
            model = "-"
        cfg = _Cfg()

        def measure(self, function, dmm_range="AUTO", nplc=None, samples=2):
            unit = {"dcv": "V", "dci": "A", "ohmf": "ohm"}.get(function, "")
            return console.ask_float("    Enter the measured value (%s): " % unit), 0.0

        def close(self):
            return None
    return _Fallback()


# --------------------------------------------------------------------------- #
# Maintenance
# --------------------------------------------------------------------------- #
class Maintenance:
    """Read-only diagnostics and whole-image calibration-memory operations.

    These procedures never alter a calibration constant -- that is the job of
    :class:`CalibrationEngine`. Note that there is no restore counterpart to the
    image backup: the working copy is overwritten from the EEPROM master at
    every boot, and only the firmware's own commit (reached by CAL END) writes
    the master, so an image can be read but not written back.
    """

    def __init__(self, gen, console: Console) -> None:
        self.gen = gen
        self.console = console

    def show_info(self) -> None:
        """Print the instrument identity and the current EXT CAL state."""
        self.console.hr()
        print(t(" INSTRUMENT"))
        self.console.hr()
        print("   %-14s %s" % (t("Resource"), self.gen.resource))
        print("   %-14s %s" % (t("Identity"), self.gen.idn or t("(not read)")))
        print("   %-14s %s" % (t("Model"), self.gen.model))
        print("   %-14s %s" % (t("Firmware"), self.gen.firmware or "-"))
        state = self.gen.ext_cal_enabled()
        print("   %-14s %s"
              % (t("EXT CAL"),
                 t("DISABLE") if state is False else
                 t("ENABLE") if state is True else
                 t("cannot be read - the instrument is not answering")))
        if active.is_bound():
            # Shown here because these govern every reading the tool takes and
            # are otherwise invisible once the file has been edited.
            print("   %-14s %s"
                  % (t("Timing"),
                     t("settle %g s, %g s on 1000 V, %g s on 100 mA, "
                       "thermal wait %g s")
                     % (active.driver.SETTLE_S, active.driver.SETTLE_HV_S,
                        active.driver.SETTLE_100MA_S,
                        active.driver.THERMAL_WAIT_S)))

    def scan_bus(self, manager: DmmManager) -> None:
        """Rescan the GPIB bus and list everything on it, generator included.

        The generator is still left out of the *scan*, but no longer for the
        reason it used to be. The old reason was that probing it corrupted the
        command that followed; measurement says otherwise -- a plain ``*IDN?``
        identifies it five times out of five with a clean status byte, which is
        why doing it by hand in Connection Expert always worked. The scanner now
        asks that way too.

        What remains is narrower: this process already holds an open session to
        the generator, and opening a second one to the same address just to ask
        it a question it can answer through the first is pointless. So it is
        asked through the session that exists, and the answer joins the listing
        in address order. It is a live reading, not a cached one.
        """
        print(t("Scanning the GPIB bus ..."))
        rows = list(manager.scan(announce=False))
        own = getattr(self.gen, "resource", "")
        if own and not any(resource == own for resource, _ in rows):
            rows.append((own, self._generator_identity()))
        if not rows:
            print(t("   nothing answered."))
            return
        for resource, identity in sorted(rows, key=_gpib_address):
            note = t("  <- generator") if resource == own else ""
            print("   %-24s %s%s"
                  % (resource, identity or t("(no response)"), note))

    def _generator_identity(self) -> str:
        """Ask the open generator session what it is, for the bus listing.

        Falls back to whatever identity was established at startup, and then to
        a plain label. The guard on the answer is not paranoia: this instrument
        returns its present output value when addressed to talk with no query
        pending, so a listing that named the generator "+0.000000 V" would be
        worse than the omission this replaced.
        """
        identity = ""
        try:
            identity = (self.gen.identify() or "").strip()
        except Exception:
            identity = ""
        if not identity or _READING_RE.match(identity):
            identity = (self.gen.idn or "").strip()
        if not identity or _READING_RE.match(identity):
            return (active.instrument.title if active.is_bound()
                    else t("the instrument"))
        return identity

    def read_cal_image(self, master: bool) -> Optional[bytes]:
        """Read and save one calibration image, showing progress."""
        which = t("EEPROM master") if master else t("working copy")
        print(t("Reading the %s (%d bytes, about two minutes) ...")
              % (which, active.driver.CAL_BLOCK_LEN))
        image = self.gen.read_cal_image(
            master=master,
            progress=lambda done, total: Console.progress_bar(
                done, total, label="cal"))
        if image is None:
            print(t("The read failed. MRB is gated to EXT CAL = DISABLE; "
                    "check the switch."))
            return None
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        name = "calimage_%s_%s.bin" % ("master" if master else "working", stamp)
        path = os.path.join(cal_images_dir(), name)
        with open(path, "wb") as handle:
            handle.write(image)
        print(t("Saved %s") % path)
        return image

    def show_cal_image(self, image: bytes) -> None:
        """Decode and print a calibration image."""
        decoded = active.driver.decode_cal_block(image)
        self.console.hr()
        print(t(" FULL-SCALE COEFFICIENTS (value in the range's panel unit)"))
        self.console.hr()
        for row in decoded["fs"]:
            print("   $%04X  %-8s %-5s %s"
                  % (row["address"], row["range"], row["point"],
                     "-- not BCD" if row["value"] is None
                     else "%.6f" % row["value"]))
        self.console.hr()
        print(t(" ZERO COEFFICIENTS (fraction of full scale)"))
        self.console.hr()
        for row in decoded["zero"]:
            print("   $%04X  %-8s %-5s %s"
                  % (row["address"], row["range"], row["point"],
                     "-- not BCD" if row["value"] is None
                     else "%.9f" % row["value"]))
        self.console.hr()
        print(t(" DAC CONNECTION COEFFICIENTS"))
        self.console.hr()
        for row in decoded["rkt"]:
            print("   $%04X  %-6s raw %s  decoded %s"
                  % (row["address"], row["selector"], row["raw"].hex(),
                     row["value"]))

    def compare_images(self) -> None:
        """Compare two saved calibration images and report what moved."""
        folder = cal_images_dir()
        files = sorted(name for name in os.listdir(folder)
                       if name.lower().endswith(".bin"))
        if len(files) < 2:
            print(t("At least two saved images are needed; found %d.")
                  % len(files))
            return
        # One list and two numbers, as menu 7 does it. Two passes over the same
        # list, one for BEFORE and one for AFTER, made the operator read the
        # same twenty filenames twice to answer what is really one question.
        rows = []
        for index, name in enumerate(files, start=1):
            try:
                stamp = datetime.datetime.fromtimestamp(
                    os.path.getmtime(os.path.join(folder, name)))
                saved = stamp.strftime("%Y-%m-%d %H:%M")
            except Exception:
                saved = "-"
            rows.append([str(index), name, saved])
        print("")
        print(t("Saved calibration images, oldest first:"))
        print("")
        for line in self.console.table(["#", t("File"), t("Saved")], rows,
                                       right=(0,)):
            print(line)
        print("")
        print(t("Enter TWO numbers, the BEFORE image first -- for example"))
        print(t('"1,2". 0 cancels.'))
        default = "%d,%d" % (len(files) - 1, len(files))
        picked = self.console.choose_numbers(
            t("Images [%s]: ") % default, len(files),
            default=default, limit=2, minimum=2)
        if picked is None:
            return
        first, second = picked
        with open(os.path.join(folder, files[first]), "rb") as handle:
            before = handle.read()
        with open(os.path.join(folder, files[second]), "rb") as handle:
            after = handle.read()
        rows = active.driver.compare_cal_blocks(before, after)
        if not rows:
            print(t("The two images are identical."))
            return
        self.console.hr()
        print("   %-22s %-8s %-16s %s"
              % (t("record"), t("address"), t("before"), t("after")))
        self.console.hr()
        for row in rows:
            print("   %-22s $%04X    %-16s %s"
                  % (row["label"], row["address"],
                     "-" if row["before"] is None else "%.9g" % row["before"],
                     "-" if row["after"] is None else "%.9g" % row["after"]))
        print("")
        print(t("%d of %d records differ.") % (len(rows), 43))


# --------------------------------------------------------------------------- #
# Calibration engine
# --------------------------------------------------------------------------- #
class CalibrationEngine:
    """Verification and adjustment.

    The procedures themselves live in :mod:`perf_engine` and
    :mod:`adjust_engine`; this class owns the shared services they need -- the
    instrument, the console, the certificate writer, the meter manager -- and
    the operations common to both, such as confirming the EXT CAL switch.
    """

    def __init__(self, gen, console: Console, certs: CertificateWriter,
                 dmm: DmmManager) -> None:
        self.gen = gen
        self.console = console
        self.certs = certs
        self.dmm = dmm

    def require_ext_cal(self, want_enabled: bool, attempts: int = 6) -> bool:
        """Confirm the EXT CAL switch is where the procedure needs it.

        Detection is now positive in **both** directions, which it was not
        before. ``*IDN?`` is gated, so:

        * DISABLE -- the query answers and the status byte's error bit is clear.
        * ENABLE -- the query is refused with Err.G15 (internal code ``$12``,
          inside the reporting range at ``$4FF7``), which sets the error bit.
          The read times out but the serial poll, which nothing gates, says why.

        A timeout with no error bit is neither position: the instrument is off,
        wrongly addressed, or the bus is wedged. That is reported as its own
        problem rather than being accepted as ENABLE, which is what an earlier
        version did -- and which let a run start against a dead bus.

        The operator is asked to move the switch and the state is then
        re-checked, rather than the answer being taken on trust.
        """
        wanted = t("ENABLE") if want_enabled else t("DISABLE")
        for attempt in range(attempts):
            state = self.gen.ext_cal_enabled()
            if state == want_enabled:
                print(t("   EXT CAL confirmed %s.") % wanted)
                return True
            print("")
            if state is None:
                print(t("The instrument is not answering and is not reporting a"))
                print(t("gated query either, so the EXT CAL position cannot be"))
                print(t("read. Check that it is powered on, at the expected GPIB"))
                print(t("address, and cabled."))
            else:
                print(t("EXT CAL currently reads %s; it must be %s.")
                      % (t("ENABLE") if state else t("DISABLE"), wanted))
                if want_enabled:
                    print(t("Use a flat-blade screwdriver or tweezers "
                            "(manual 7.2.2(2))."))
                else:
                    print(t("With it in ENABLE every ordinary command is "
                            "refused with Err.G15."))
            if attempt == attempts - 1:
                break
            if not self.console.yesno(
                    t("Set it to %s and check again?") % wanted, default=True):
                return False
        print(t("EXT CAL could not be confirmed as %s.") % wanted)
        return False

    def read_cal_image(self, master: bool = False) -> Optional[bytes]:
        """Read a calibration image, save it, and return it.

        The image is written to ``cal_images/`` before it is returned. An
        adjustment's before-image is the only record of the coefficients that
        CAL END is about to overwrite, and there is no undo, so keeping it only
        in memory means losing it to any crash, any exception, and the end of
        the session. Saving is not conditional on anything.
        """
        which = "master" if master else "working"
        print(t("   reading the calibration block (%s) ...") % which)
        image = self.gen.read_cal_image(
            master=master,
            progress=lambda done, total: Console.progress_bar(
                done, total, label="cal"))
        if image is None:
            print(t("   the read failed; MRB is gated to EXT CAL = DISABLE."))
            return None
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(cal_images_dir(),
                            "calimage_%s_%s.bin" % (which, stamp))
        try:
            with open(path, "wb") as handle:
                handle.write(image)
            print(t("   saved %s") % path)
        except OSError as exc:
            # Report loudly but still return the image: an unsaved before-image
            # is much worse than no before-image only if the caller thinks it
            # was saved.
            print(t("   COULD NOT SAVE THE IMAGE (%s). It exists only in memory"
                    " for this session.") % exc)
        return image

    def report_cal_change(self, before: bytes, after: bytes) -> None:
        """Print what a commit changed, as evidence that it landed."""
        rows = active.driver.compare_cal_blocks(before, after)
        if not rows:
            print(t("No calibration record changed. CAL END may not have been "
                    "accepted; check the panel for an error."))
            return
        self.console.hr()
        print(t(" CALIBRATION MEMORY CHANGED - %d records") % len(rows))
        self.console.hr()
        for row in rows:
            print("   %-22s $%04X  %-16s -> %s"
                  % (row["label"], row["address"],
                     "-" if row["before"] is None else "%.9g" % row["before"],
                     "-" if row["after"] is None else "%.9g" % row["after"]))

    def run_verification(self, rc: ReportConfig, short: bool) -> None:
        """Run a performance verification."""
        if perf_engine is None:
            print(t("The verification engine is unavailable."))
            return
        perf_engine.run(self, rc, short=short, interval=rc.interval)

    def run_adjustment(self, rc: ReportConfig) -> None:
        """Run the full adjustment."""
        if adjust_engine is None:
            print(t("The adjustment engine is unavailable."))
            return
        adjust_engine.run(self, rc)


# --------------------------------------------------------------------------- #
# Menu controller
# --------------------------------------------------------------------------- #
def _gpib_address(row: Tuple[str, Optional[str]]) -> Tuple[int, str]:
    """Sort key putting a bus listing in address order rather than scan order.

    The generator is added to the listing from what discovery found rather than
    by probing it again, so it would otherwise always appear last however low
    its address. Anything without a recognisable numeric address sorts after
    everything that has one, by name.
    """
    match = re.search(r"::(\d+)::", row[0] or "")
    return (int(match.group(1)) if match else 10 ** 6, row[0] or "")


def _duration(module, name: str) -> str:
    """Return an engine's own estimate of how long it takes, for the menu.

    Asked of the engine rather than typed here, so the figure follows the
    settling times in the USER SETTINGS block instead of contradicting them.
    An engine that failed to import, or that cannot answer, leaves the column
    blank rather than the menu unprintable.
    """
    try:
        return t("(about %d minutes)") % getattr(module, name)()
    except Exception:
        return ""


#: The menu is built rather than printed, because which items exist depends on
#: what the instrument's plugin says it can do. An instrument whose calibration
#: commands are undocumented declares ``adjust = no`` and simply has no
#: adjustment item -- rather than one that exists and fails when it is chosen.
#:
#: Numbering is fixed, not sequential: item 4 is the adjustment on every
#: instrument that has one, so an operator who knows the tool does not have to
#: re-learn the numbers per instrument, and a note in a runsheet stays true.
MENU_ITEMS = (
    ("1", "info", "Instrument information", None),
    ("2", "scan", "Scan the GPIB bus", None),
    (None, None, None, None),
    ("3", "verify", "Performance verification", "verify"),
    ("4", "adjust", "Full adjustment", "adjust"),
    (None, None, None, None),
    ("5", "readcal", "Read and decode the calibration memory", "calram"),
    ("6", "compare", "Compare two saved calibration images", "calram"),
    (None, None, None, None),
    ("7", "report", "Generate a calibration certificate", None),
    ("8", "language", "Interface language", None),
    (None, None, None, None),
    ("0", "quit", "Quit", None),
)

MENU_HEADER = """
==========================================================================
 Service, Verification and Adjustment Tool                       v%s
==========================================================================
 %s
 Connected: %s
"""


def build_menu(instrument, idn: str) -> Tuple[str, Dict[str, str]]:
    """Return the menu text and the map from keystroke to action.

    The map is what the loop dispatches on, so an item that was not offered
    cannot be reached by typing its number either.
    """
    title = (instrument.title if instrument is not None
             else t("no instrument selected"))
    lines = [MENU_HEADER % (VERSION, title, idn)]
    actions: Dict[str, str] = {}
    durations = {"verify": _duration(perf_engine, "estimated_minutes"),
                 "adjust": _duration(adjust_engine, "estimated_minutes")}
    for key, action, label, capability in MENU_ITEMS:
        if key is None:
            lines.append("")
            continue
        if capability and (instrument is None or not instrument.can(capability)):
            continue
        suffix = durations.get(action, "")
        lines.append(("   %s) %-38s %s" % (key, label, suffix)).rstrip())
        actions[key] = action
    # Collapse the blank runs left where a whole group was omitted, so a
    # verify-only instrument does not show a gap where its adjustment would be.
    tidy = []
    for line in lines:
        if line == "" and tidy and tidy[-1] == "":
            continue
        tidy.append(line)
    return "\n".join(tidy) + "\n", actions



class ServiceTool:
    """The composition root and the menu loop."""

    def __init__(self, gen) -> None:
        self.gen = gen
        self.console = Console()
        self.session_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.certs = CertificateWriter(gen, self.session_id)
        self.dmm = DmmManager(gen, self.console)
        self.maintenance = Maintenance(gen, self.console)
        self.engine = CalibrationEngine(gen, self.console, self.certs, self.dmm)

    # ---- connection --------------------------------------------------------- #
    @classmethod
    def connect(cls, resource: str = DEFAULT_RESOURCE) -> Optional["ServiceTool"]:
        """Open the generator and return a ready tool, or ``None``."""
        if not active.is_bound():
            print(t("No instrument is selected."))
            return None
        print(t("Connecting to %s ...") % resource)
        try:
            gen = active.instrument.open(resource)
        except Exception as exc:
            print(t("Could not open %s: %s") % (resource, exc))
            return None
        try:
            gen.identify()
            print(t("   %s") % gen.idn)
        except Exception:
            print(t("   the instrument did not answer *IDN?. That query is "
                    "gated to EXT CAL = DISABLE, so check the rear-panel "
                    "switch."))
        return cls(gen)

    # ---- report generation -------------------------------------------------- #
    @staticmethod
    def _run_aspect(run: dict) -> str:
        """Return ``asfound``, ``asleft`` or ``adjust`` for a saved run."""
        return (run.get("data") or {}).get("aspect", "asfound")

    @staticmethod
    def _clone(payload: dict) -> dict:
        """Deep-copy through JSON so composing a report never mutates the file."""
        return json.loads(json.dumps(payload))

    def _find_companion(self, saved: List[dict], chosen: dict,
                        aspect: str) -> Optional[dict]:
        """Find the newest run of the opposite aspect for the same instrument.

        A run from the same session is preferred, because that is almost always
        the pairing the operator means.
        """
        wanted = "asleft" if aspect == "asfound" else "asfound"
        candidates = [r for r in saved
                      if r is not chosen
                      and r.get("serial") == chosen.get("serial")
                      and self._run_aspect(r) == wanted]
        if not candidates:
            return None
        same_session = [r for r in candidates
                        if r.get("session_id") == chosen.get("session_id")]
        return (same_session or candidates)[0]

    def _compose_single(self, run: dict) -> dict:
        """Build certificate data from one run."""
        data = self._clone(run["data"])
        aspect = self._run_aspect(run)
        if aspect == "asfound":
            data["as_found_results"] = data.pop("results", [])
            data["test_type_key"] = "type_asfound"
        elif aspect == "asleft":
            data["as_left_results"] = data.pop("results", [])
            data["test_type_key"] = "type_asleft"
        else:
            data["test_type_key"] = "type_asleft"
        data["overall_pass"] = cal_report.overall_verdict(
            data.get("as_left_results") or data.get("as_found_results")
            or data.get("results") or [])
        return data

    def _compose_combined(self, found: dict, left: dict) -> dict:
        """Merge an as-found and an as-left run into one certificate."""
        data = self._clone(left["data"])
        data["as_found_results"] = self._clone(found["data"]).get("results", [])
        data["as_left_results"] = data.pop("results", [])
        data["test_type_key"] = "type_combined"
        seen, standards = set(), []
        for source in (found, left):
            for entry in (source["data"].get("standards") or []):
                key = (entry.get("description"), entry.get("model"))
                if key not in seen:
                    seen.add(key)
                    standards.append(entry)
        data["standards"] = standards
        conditions = dict(data.get("conditions") or {})
        conditions["procedure"] = (
            "%s\n\nAs-found: %s\nAs-left: %s"
            % ((left["data"].get("conditions") or {}).get("procedure", ""),
               found.get("cal_date", ""), left.get("cal_date", "")))
        data["conditions"] = conditions
        labels = dict((found["data"].get("section_labels") or {}))
        labels.update(left["data"].get("section_labels") or {})
        data["section_labels"] = labels
        # The overall verdict is the condition the instrument LEAVES in, so it
        # comes from the as-left rows. A combined certificate that reported PASS
        # while its as-left rows failed would be worse than no certificate.
        data["overall_pass"] = cal_report.overall_verdict(
            data.get("as_left_results") or [])
        return data

    def _natural_pair(self, saved: List[dict]) -> Optional[Tuple[int, int]]:
        """Return the indices of the obvious as-found/as-left pair, if there is one.

        Offered as the prompt's default so the commonest case -- verify, adjust,
        verify, certify -- is a single keypress, while any other pairing is still
        two numbers typed by hand.
        """
        for run in saved:
            companion = self._find_companion(saved, run, self._run_aspect(run))
            if companion is None:
                continue
            first = next(i for i, r in enumerate(saved) if r is run)
            second = next(i for i, r in enumerate(saved) if r is companion)
            return (min(first, second), max(first, second))
        return None

    def _confirm_serials(self, first: dict, second: dict) -> bool:
        """Warn, and require confirmation, before combining two instruments."""
        if first.get("serial") == second.get("serial"):
            return True
        print("")
        print(t("Those two runs have different serial numbers: %s and %s.")
              % (first.get("serial", "?"), second.get("serial", "?")))
        print(t("A certificate covers one instrument, so combining them"))
        print(t("would describe two as though they were one."))
        return self.console.yesno(t("Combine them anyway?"), default=False)

    def _combine(self, first: dict, second: dict) -> dict:
        """Combine two runs, working out which is the as-found of the pair.

        The aspect tags decide it where they disagree, and the save times decide
        it where they do not -- two runs both tagged as-found are still a
        before and an after, and the earlier one is the before.
        """
        first_aspect = self._run_aspect(first)
        second_aspect = self._run_aspect(second)
        if first_aspect != second_aspect:
            found, left = ((first, second) if first_aspect == "asfound"
                           else (second, first))
        else:
            found, left = sorted(
                (first, second), key=lambda run: run.get("saved_at", ""))
            print("")
            print(t("Both runs are tagged %s, so the earlier one is shown as")
                  % first_aspect)
            print(t("as-found and the later one as as-left."))
        return self._compose_combined(found, left)

    @staticmethod
    def _run_point_count(run: dict) -> str:
        """Return how many measured points a saved run holds, as text."""
        data = run.get("data") or {}
        rows = (data.get("results") or data.get("as_left_results")
                or data.get("as_found_results") or [])
        return str(len(rows)) if rows else "-"

    def _print_saved_runs(self, saved: List[dict]) -> None:
        """Print the numbered table the operator picks from.

        The serial and test-type columns appear only when the saved runs differ
        in them. A column of identical values is width spent on nothing -- and
        on a bench that calibrates one R6161 over and over, both of those
        columns are exactly that. Where they do differ, the serial is the most
        important thing on the screen; where they do not, a line under the
        table says so once.
        """
        tags = {run.get("tag", "") for run in saved}
        serials = {run.get("serial", "") for run in saved}
        show_tag, show_serial = len(tags) > 1, len(serials) > 1

        header = ["#", t("Saved")]
        if show_tag:
            header.append(t("Test"))
        header.append(t("Aspect"))
        if show_serial:
            header.append(t("Serial"))
        header += [t("Points"), t("Certificate")]

        rows = []
        for index, run in enumerate(saved, start=1):
            row = [str(index),
                   (run.get("saved_at", "") or "").replace("T", " ")[:16]]
            if show_tag:
                row.append(TAG_SHORT.get(run.get("tag", ""),
                                         run.get("tag", "")))
            row.append(ASPECT_LABELS.get(self._run_aspect(run),
                                         self._run_aspect(run)))
            if show_serial:
                row.append(run.get("serial", ""))
            row += [self._run_point_count(run), run.get("cert_number", "")]
            rows.append(row)

        print("")
        print(t("Saved runs, newest first:"))
        print("")
        # Only the number and the point count are right-aligned; certificate
        # numbers are read as text, not compared as quantities.
        for line in self.console.table(header, rows,
                                       right=(0, len(header) - 2)):
            print(line)

        # What every row has in common, said once, since the columns for it
        # were dropped. Phrased so that it reads correctly over a list of one.
        common = []
        if not show_tag:
            common.append(TAG_LABELS.get(tags.pop(), t("saved run")))
        if not show_serial:
            common.append(t("serial %s") % (serials.pop() or t("unknown")))
        if common:
            print("")
            print(t("   Every run listed: %s") % ", ".join(common))

    def generate_report(self, rc: ReportConfig) -> None:
        """List saved runs and produce a certificate from one or two of them."""
        saved = CertificateWriter.list_saved()
        if not saved:
            print(t("No saved runs. Run a verification first."))
            return

        self._print_saved_runs(saved)

        # One list, and any two of its rows can be paired: an as-found with its
        # as-left, a verification from a previous visit with today's, a rerun
        # after a repair. Two numbers is the whole interface -- no follow-up
        # question about whether to combine, and no second list whose numbering
        # differs from the first because one row has been removed from it.
        pair = self._natural_pair(saved) if len(saved) > 1 else None
        default = "%d,%d" % (pair[0] + 1, pair[1] + 1) if pair else "1"
        print("")
        if len(saved) > 1:
            print(t("Enter ONE number for a single certificate, or TWO numbers"))
            print(t('such as "1,3" for a combined as-found and as-left one.'))
            if pair:
                print(t("Runs %d and %d are the %s and the %s of one")
                      % (pair[0] + 1, pair[1] + 1,
                         ASPECT_LABELS.get(self._run_aspect(saved[pair[0]]), "?"),
                         ASPECT_LABELS.get(self._run_aspect(saved[pair[1]]), "?")))
                print(t("instrument, so they are offered as the default."))
        else:
            print(t("Enter the number of the run to certify."))
        print(t("0 cancels."))

        while True:
            picked = self.console.choose_numbers(
                t("Run(s) [%s]: ") % default, len(saved), default=default)
            if picked is None:
                return
            if len(picked) == 1:
                data = self._compose_single(saved[picked[0]])
                break
            first, second = saved[picked[0]], saved[picked[1]]
            if not self._confirm_serials(first, second):
                continue
            data = self._combine(first, second)
            break

        languages = self._choose_languages(rc)
        if not languages:
            return
        basename = data.get("cert_number", "certificate")
        written = self.certs.generate_pdfs(rc, data, basename, languages)
        for path in written:
            print(t("   wrote %s") % path)
        if not written:
            print(t("No certificate was produced."))

    def _choose_languages(self, rc: ReportConfig) -> List[str]:
        """Offer the installed report languages, defaulting to the configured set."""
        available = i18n.available_report_languages()
        if not available:
            print(t("No report language catalogs are installed."))
            return []
        default = [code for code in rc.languages if code in available] or ["en"]
        print("")
        print(t("Report languages available: %s") % ", ".join(available))
        answer = self.console.ask(
            t("Languages to produce, comma separated [%s]: ") % ",".join(default),
            default=",".join(default))
        chosen = [code.strip() for code in answer.split(",") if code.strip()]
        unknown = [code for code in chosen if code not in available]
        for code in unknown:
            print(t("   no catalog for %r; skipping it.") % code)
        return [code for code in chosen if code in available]

    def choose_ui_language(self) -> None:
        """Change the interface language for the rest of the session."""
        available = i18n.available_ui_languages()
        if not available:
            print(t("No interface language catalogs are installed."))
            return
        labels = ["%s (%s)" % (i18n.language_name(code), code)
                  for code in available]
        index = self.console.choose(t("Language: "), labels,
                                    available.index(i18n.ui_language())
                                    if i18n.ui_language() in available else 0)
        if index is None:
            return
        i18n.set_ui_language(available[index])
        print(t("Interface language set to %s.")
              % i18n.language_name(available[index]))

    # ---- the loop ----------------------------------------------------------- #
    def run(self) -> None:
        """Run the menu until the operator quits."""
        # Only if nobody has already handed one over. main() scans during
        # discovery and passes the result in, and scanning the bus again for the
        # same answer is a visible pause with nothing to show for it -- the
        # operator sees the tool appear to do the same thing twice before the
        # menu arrives. Menu item 2 still forces a fresh scan on demand.
        if not self.dmm.bus_scan:
            self.dmm.scan(announce=False)
        while True:
            rc = ReportConfig.load()     # reload so .ini edits apply immediately
            menu, actions = build_menu(active.instrument,
                                       self.gen.idn or self.gen.resource)
            print(menu)
            choice = self.console.ask(t("Select: "))
            action = actions.get(choice.strip())
            try:
                if action is None:
                    print(t("Unknown selection."))
                elif action == "info":
                    self.maintenance.show_info()
                elif action == "scan":
                    self.maintenance.scan_bus(self.dmm)
                elif action == "verify":
                    self.engine.run_verification(rc, short=False)
                elif action == "adjust":
                    self.engine.run_adjustment(rc)
                elif action == "readcal":
                    master = self.console.yesno(
                        t("Read the EEPROM master rather than the working copy?"),
                        default=False)
                    image = self.maintenance.read_cal_image(master)
                    if image is not None:
                        self.maintenance.show_cal_image(image)
                elif action == "compare":
                    self.maintenance.compare_images()
                elif action == "report":
                    self.generate_report(rc)
                elif action == "language":
                    self.choose_ui_language()
                elif action == "quit":
                    print(t("Putting the output in standby and closing."))
                    try:
                        self.gen.standby()
                    except Exception:
                        pass
                    # A meter session is held open for the length of a run and
                    # released when it ends, but a run that was interrupted may
                    # not have got that far. Nothing should hold the bus after
                    # the tool has quit.
                    self.dmm.close_all()
                    self.gen.close()
                    break
            except KeyboardInterrupt:
                print(t("\nInterrupted."))
            except Exception as exc:
                # Deliberately broad: a fault in one procedure must not end the
                # session and lose the operator's other saved work.
                print(t("Error: %s") % exc)
            self.console.pause()


#: Substring that identifies the generator in an identification response.
#: Retired: which identity strings mean "this instrument" is the plugin's to
#: say, in [instrument] identity_match. Kept as the fallback for a scan run
#: before an instrument has been chosen.
GENERATOR_ID_MATCH = "R6161"


def _generator_matches(identity) -> bool:
    """Is this identity the instrument this session is working on?"""
    if active.is_bound():
        return active.instrument.plugin.matches(identity)
    return bool(identity) and GENERATOR_ID_MATCH in str(identity).upper()

#: A response that is a DC reading rather than an identity: "+0.000000 V",
#: "-1.234560 mA" and so on. The generator answers a generic probe with its
#: present output, so this is what it looks like from the outside.
_READING_RE = re.compile(r"^\s*[+-]?\d+\.\d+\s*(?:[munkM]?[VA])?\s*$")

#: Addresses found already flagging an error before this tool sent anything.
#: Filled by :func:`_confirm_generator` and reported once by :func:`discover`.
_PRE_EXISTING_ERROR: List[str] = []


def _confirm_generator(resource: str) -> Optional[str]:
    """Open ``resource`` with the real driver and ask what it is.

    Returns the identity string if it is an R6161, otherwise ``None``.

    A generic probe can miss this instrument even though it is present and
    healthy: a reading left in its output buffer by an earlier session is
    returned ahead of the answer to any query, so the scanner reads
    ``+0.000000 V`` where the identity should be. The driver's sacrificial read
    consumes that stale value first, which is why a session opened through it
    gets a straight answer where a raw one does not.

    Opens and closes cleanly, and never raises.
    """
    if not active.is_bound():
        return None
    gen = None
    try:
        gen = active.instrument.open(resource)
        # Poll BEFORE sending anything. The error bit is cleared by the next
        # line that completes cleanly, and identify() is such a line, so this is
        # the only moment an error that was already there can be seen. It
        # settles a question that would otherwise be unanswerable: whether a
        # fault on the panel is one this tool caused or one it inherited.
        try:
            if gen.poll_status() & active.driver.STB_ERROR_BIT:
                _PRE_EXISTING_ERROR.append(resource)
        except Exception:
            pass
        identity = gen.identify() or ""
        return identity if _generator_matches(identity) else None
    except Exception:
        return None
    finally:
        try:
            if gen is not None:
                gen.close()
        except Exception:
            pass


def discover(console: Console):
    """Scan the bus once and work out where the generator is.

    Returns ``(resource, scan)``: the generator's VISA resource and the full
    scan, so the meter manager can reuse it instead of scanning a second time.

    Two things stop a plain scan from finding this instrument, and both are
    properties of the R6161 rather than of the scan:

    * it refuses every query outright while the rear-panel EXT CAL switch is in
      ENABLE, so a healthy instrument can be genuinely silent
    * a reading left in its output buffer by an earlier session is returned
      ahead of the answer to any query, so a probe can come back as
      ``+0.000000 V`` -- which is why a response shaped like a reading is
      treated as a candidate rather than as a different instrument

    **Two separate things made a scan raise Err.G07 on it**, and neither was
    the identify query. Both were measured on the bench, five fresh sessions
    apiece with the status byte polled after each:

    * ``ID?``, the pre-SCPI fallback a 3458A needs, is read by the R6161 as the
      current-range command ``I`` with a range value of ``D?`` -- an invalid X
      for ``IX``, which is exactly what G07 means. It is never safe to send.
    * ``*IDN?`` is perfectly safe **on its own** -- 5/5, status clean -- but not
      after a write-like bus operation. A device clear before it fails 5/5;
      asserting REN before it fails 4/5. Both leave the next command a character
      short, so ``*IDN?`` arrives as ``IDN?`` and lands on the same ``I``
      handler. The scanner used to do both before every probe.

    That the query itself was innocent was visible all along from outside the
    tool: typing ``*IDN?`` into Keysight Connection Expert answers correctly
    every time, because an operator typing a query by hand does not send a
    device clear first. The scanner now asks the same way -- see
    :func:`dmm_driver.scan_bus`, which probes plainly and escalates to REN and a
    device clear only for instruments that stay silent.

    So the generator is identified by the ordinary scan, with nothing done to it
    that it could object to. The bus is still scanned in two passes, because the
    *other* cause has not gone away: ``ID?`` must never reach the R6161.

    1. ``*IDN?`` everywhere. Enough to identify the generator.
    2. ``ID?`` only where pass 1 found nothing, and never at the generator's
       address -- which is what finds a 3458A without troubling the R6161.

    The error was harmless and the instrument was fine, but a tool that lights a
    fault on the panel every time it starts is training its operator to ignore
    the panel.
    """
    if dmm_driver is None:
        return DEFAULT_RESOURCE, []

    while True:
        print(t("Scanning the GPIB bus ..."))
        try:
            scan, generator = _two_pass_scan(console)
        except Exception as exc:
            print(t("   the bus scan failed (%s); assuming %s")
                  % (exc, DEFAULT_RESOURCE))
            return DEFAULT_RESOURCE, []

        for resource, identity in scan:
            print("   %-24s %s" % (resource, identity or t("(no response)")))

        meters = [path for path in dmm_driver.list_configs()
                  if dmm_driver.match_config(path, scan)]

        if dmm_driver.CONFIG_ERRORS:
            print("")
            print(t("   NOTE: these DMM config files could not be read and were"))
            print(t("   skipped. The scan continued without them:"))
            for message in dmm_driver.CONFIG_ERRORS:
                print("     %s" % message)
            del dmm_driver.CONFIG_ERRORS[:]

        if _PRE_EXISTING_ERROR:
            print("")
            print(t("   NOTE: %s was already flagging an error before this tool")
                  % _PRE_EXISTING_ERROR[0])
            print(t("   sent it anything, so whatever is on the panel was not"))
            print(t("   caused by the scan. It clears at the next clean command."))
            del _PRE_EXISTING_ERROR[:]

        if generator and meters:
            print(t("   %s confirmed at %s")
                      % (active.instrument.title if active.is_bound()
                         else t("instrument"), generator))
            for path in meters:
                cfg = dmm_driver.DMMConfig(path)
                hit = dmm_driver.match_config(path, scan)
                print(t("   reference: %s at %s") % (cfg.name, hit[0]))
            return generator, scan

        # Something the run cannot proceed without is missing. Say which, and
        # why it might be missing, rather than reporting a bare failure.
        print("")
        console.hr()
        if not generator:
            print(t(" NO %s FOUND") % (active.instrument.title.upper()
                                   if active.is_bound() else "INSTRUMENT"))
            console.hr()
            print(t("Every query is refused while the rear-panel EXT CAL switch"))
            print(t("is in ENABLE, so a healthy instrument is silent in that"))
            print(t("position. Check the switch before you check the cable."))
        if not meters:
            if not generator:
                print("")
            print(t(" NO RECOGNISED REFERENCE MULTIMETER"))
            console.hr()
            print(t("Nothing on the bus matched a file in dmm_configs/. Check"))
            print(t("that the meter is powered, addressed and in remote, or add"))
            print(t("a config for it -- _EXAMPLE.ini documents every key."))
        print("")
        print(t("  [R] rescan          connect it and try again"))
        print(t("  [C] continue        go on anyway; you will be asked instead"))
        print(t("  [Q] quit"))
        answer = console.ask(t("  Choice [R]: "), default="R").strip().lower()
        if answer.startswith("q"):
            return None, scan
        if answer.startswith("c"):
            if generator:
                return generator, scan
            resource = console.ask(
                t("VISA resource for the %s [%s]: ")
                % (active.instrument.name if active.is_bound()
                   else t("instrument"), DEFAULT_RESOURCE),
                default=DEFAULT_RESOURCE)
            return resource, scan
        print("")


def _two_pass_scan(console: Console):
    """Enumerate the bus without provoking Err.G07 on the generator.

    Returns ``(scan, generator_resource_or_None)``.

    ``ID?`` -- the pre-SCPI fallback a 3458A needs -- is the probe the R6161
    reads as a malformed current-range command, so it must never reach it.
    Which means knowing where the generator is *before* sending it anywhere.

    Normally that is one probe: the configured address, asked directly through
    the driver. Everything else can then be scanned once, in the ordinary way.
    Only when the generator is somewhere unexpected does this fall back to
    scanning twice -- ``*IDN?`` everywhere to locate it, then ``ID?`` at the
    addresses that stayed silent.
    """
    # Fast path. Only the generator needs `ID?` withheld, and it is almost
    # always where it is configured to be, so ask that one address directly --
    # a single safe probe through the driver -- and let one ordinary scan cover
    # everything else. That is one enumeration of the bus instead of two, which
    # on a five-address bus with three empty slots is the difference between
    # about ten seconds of startup and about thirty.
    identity = _confirm_generator(DEFAULT_RESOURCE)
    if identity:
        rest = dmm_driver.scan_bus(skip=[DEFAULT_RESOURCE])
        return [(DEFAULT_RESOURCE, identity)] + rest, DEFAULT_RESOURCE

    # Slow path: the generator is not where it was expected, so `ID?` cannot be
    # sent anywhere until we know which address to withhold it from.
    first = dmm_driver.scan_bus(queries=("*IDN?",))
    generator, confirmed_identity = _resolve_generator(first, console)
    if generator and confirmed_identity:
        # It was found by re-probing, not by the scan, so the scan's own entry
        # still says "no response". Correcting it matters: a listing that
        # reports nothing at an address the next line confirms an instrument at
        # is the kind of contradiction that makes an operator distrust the tool.
        first = [(resource, confirmed_identity if resource == generator
                  else found) for resource, found in first]

    identified = {resource for resource, identity in first if identity}
    skip = set(identified)
    # If the generator did not answer -- EXT CAL in ENABLE -- its configured
    # address is still not a safe place to send ID?.
    skip.add(generator or DEFAULT_RESOURCE)

    merged = dict(first)
    remaining = [resource for resource, identity in first
                 if not identity and resource not in skip]
    if remaining:
        for resource, identity in dmm_driver.scan_bus(queries=("ID?",),
                                                      skip=sorted(skip)):
            if identity:
                merged[resource] = identity

    scan = [(resource, merged.get(resource)) for resource, _ in first]
    return scan, generator


def _resolve_generator(scan, console: Console):
    """Find the generator in a scan.

    Returns ``(resource, identity)``. ``identity`` is set only when the
    generator had to be confirmed by re-probing, so the caller can correct a
    scan entry that says nothing answered.
    """
    named = [resource for resource, identity in scan
             if _generator_matches(identity)]
    if named:
        if len(named) == 1:
            return named[0], None
        print("")
        print(t("More than one %s answered.")
              % (active.instrument.name if active.is_bound() else t("instrument")))
        index = console.choose(t("Generator: "), named, 0)
        return (named[index] if index is not None else None), None

    # Nothing named itself. Anything that answered with a reading is a
    # candidate -- a stale value in its output buffer looks exactly like that --
    # and so is the configured default, in case it answered nothing at all.
    candidates = [resource for resource, identity in scan
                  if identity and _READING_RE.match(identity)]
    if DEFAULT_RESOURCE not in candidates:
        candidates.append(DEFAULT_RESOURCE)
    for resource in candidates:
        identity = _confirm_generator(resource)
        if identity:
            return resource, identity
    return None, None



# --------------------------------------------------------------------------- #
# Choosing the instrument
# --------------------------------------------------------------------------- #
def choose_instrument(console: Console,
                      scan: Optional[List[Tuple[str, Optional[str]]]] = None
                      ) -> Optional[instruments.Instrument]:
    """Pick an instrument plugin, load it and bind it.

    The bus decides where it can. A scan that found exactly one supported
    instrument is not a question -- asking anyway trains the operator to press
    Enter without reading, which is how the wrong instrument eventually gets
    picked. Two supported instruments on one bus IS a question, and is put to
    them.

    Returns ``None`` if there is nothing to work with, having already said why.
    """
    plugins, errors = instruments.discover()
    for message in errors:
        print(t("   instrument plugin problem: %s") % message)
    if not plugins:
        print(t("No instrument plugins found in %s.") % instruments.plugins_dir())
        print(t("Each instrument is a folder there holding an instrument.ini"))
        print(t("and the modules it names."))
        return None

    hits = instruments.match_scan(plugins, scan or [])
    chosen: Optional[instruments.Plugin] = None
    if len(hits) == 1:
        chosen, resource, identity = hits[0]
        print(t("   %s recognised at %s") % (chosen.title, resource))
    elif len(hits) > 1:
        print("")
        print(t("More than one supported instrument answered:"))
        labels = ["%-28s %s" % (p.title, res) for p, res, _ in hits]
        index = console.choose(t("Instrument: "), labels, 0)
        if index is None:
            return None
        chosen = hits[index][0]
    else:
        # Nothing matched -- either no scan, or nothing on the bus this tool
        # knows. Offer what it supports rather than stopping: the operator may
        # be about to fix a cable, or the instrument may be gating its identity
        # the way an R6161 does with EXT CAL in ENABLE.
        print("")
        print(t("No supported instrument was recognised on the bus."))
        print(t("These plugins are installed:"))
        labels = ["%-30s %s" % (p.title,
                                ", ".join(sorted(p.capabilities)) or "nothing")
                  for p in plugins]
        index = console.choose(t("Work with: "), labels, 0)
        if index is None:
            return None
        chosen = plugins[index]

    try:
        loaded = chosen.load()
    except instruments.PluginError as exc:
        print(t("   that plugin cannot be used: %s") % exc)
        return None
    active.bind(loaded)
    capture_timing_defaults()
    return loaded


def main() -> int:
    """Entry point."""
    global DEFAULT_RESOURCE
    i18n.lang_dir()
    i18n.set_ui_language(UI_LANGUAGE)
    print(t("Calibration Service Tool v%s") % VERSION)

    console = Console()

    # Scan first, choose second. The bus is what says which instrument is
    # actually there, and a tool that asked before looking would be asking a
    # question it could have answered.
    print(t("Scanning the GPIB bus ..."))
    try:
        scan = dmm_driver.scan_bus() if dmm_driver is not None else []
    except Exception as exc:
        print(t("   the bus scan failed (%s)") % exc)
        scan = []
    for resource_seen, identity in scan:
        print("   %-24s %s" % (resource_seen, identity or t("(no response)")))

    instrument = choose_instrument(console, scan)
    if instrument is None:
        return 1
    DEFAULT_RESOURCE = instrument.plugin.default_resource

    # Timing settings are pushed into the instrument that was just bound, so
    # this cannot run before the choice is made.
    for note in apply_user_settings():
        print(t("   %s") % note)

    resource, scan = discover(console)
    if resource is None:
        return 1

    tool = ServiceTool.connect(resource)
    if tool is None:
        return 1

    # Hand the scan over rather than repeating it. The generator's own entry is
    # dropped: probing it again with a write would corrupt the command that
    # follows, which is why DmmManager.scan skips it too.
    tool.dmm.bus_scan = [(res, identity) for res, identity in scan
                         if res.upper() != resource.upper()]

    tool.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
