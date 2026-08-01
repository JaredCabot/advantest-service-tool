"""Config-driven reference-multimeter driver for the R6161 service tool.

Supports GPIB, USB and LAN (TCPIP) instruments through PyVISA. The command set
is described by a human-readable ``.ini`` file, so any sufficiently accurate
meter can be used by editing or adding a config; ready-made configs live in
``dmm_configs/``.

Three measurement functions are needed, one per hookup group of the R6161
procedures, plus one convenience function:

``DCV``   DC volts   -- the main and Divider output groups
``DCI``   DC amps    -- the current group
``OHMF``  4-wire ohms -- optional, to measure the current-limit standard
                        resistor so its real value is used instead of nominal

Unlike a fixed-function driver, each reading may name a **range** and an
integration time in power-line cycles, because the R6161 procedures step from
10 microvolts to 1000 volts and no single range setting serves both ends.
Configs express this with ``{range}`` and ``{nplc}`` placeholders.

Public surface
--------------
:class:`DMMConfig`   -- parsed ``.ini`` accessor.
:class:`DMM`         -- a live, config-described meter session.
:class:`ManualDMM`   -- keyboard entry, for a meter with no remote interface.
:func:`configs_dir` / :func:`list_configs` -- locate the bundled configs.
:func:`scan_bus`     -- enumerate and identify every instrument on the bus.
:func:`match_config` -- pair a config with a scan hit by its identity string.
"""

from __future__ import annotations

import os
import re
import time
import configparser
from typing import Dict, List, Optional, Sequence, Tuple

try:
    import pyvisa
except ImportError:  # pyvisa is optional at import time; required to open a DMM.
    pyvisa = None


#: The measurement functions a config may describe. Each maps to a config
#: section of the same (lower-cased) name.
FUNCTIONS: Tuple[str, ...] = ("dcv", "dci", "ohmf")


# --------------------------------------------------------------------------- #
# Read timeout
#
# A fixed ``timeout_ms`` cannot survive someone changing ``nplc``: the time one
# reading takes is dominated by the integration aperture, which is
# ``nplc / line frequency`` seconds and scales without limit. A config written
# for 20 PLC and then set to 200 will time out on every reading and look like a
# dead meter.
#
# The timeout is therefore derived from the integration time actually in use,
# and ``timeout_ms`` becomes a floor rather than the value.
#
#     timeout = (nplc / line_freq) x apertures + overhead + settle
#
# The multiplier is the number of whole apertures one reading costs. It is more
# than one because an auto-zero cycle is itself a full-length integration, and
# the meter does its own settling around both. Measured on the 3458A on 28 July
# 2026: at NPLC 100 and 50 Hz -- a 2 s aperture -- one reading took 9.9 s, so
# about five apertures. Eight is used, to leave room for a slower meter.
# --------------------------------------------------------------------------- #

#: Assumed mains frequency when a config does not state one. 50 Hz is the
#: conservative choice: the aperture is ``nplc / line_freq``, so assuming the
#: lower frequency over-estimates the time a reading needs rather than under.
DEFAULT_LINE_FREQ_HZ = 50.0

#: Whole integration apertures one reading is assumed to cost.
DEFAULT_NPLC_APERTURES = 8.0

#: Fixed allowance on top, for command handling, the drain and transfer.
DEFAULT_READ_OVERHEAD_S = 15.0

#: An upper bound, so a mistyped ``nplc`` cannot produce a timeout measured in
#: hours. At the 3458A's maximum of 1000 PLC the derived value is about three
#: minutes, well inside this.
TIMEOUT_CEILING_MS = 600000


# --------------------------------------------------------------------------- #
# Small text helpers
# --------------------------------------------------------------------------- #
def _decode_escapes(text: str) -> str:
    """Turn the literal ``\\r`` / ``\\n`` / ``\\t`` written in an ``.ini`` value
    into the real control characters PyVISA expects for terminations."""
    return text.replace("\\r", "\r").replace("\\n", "\n").replace("\\t", "\t")


def _lines(block: str) -> List[str]:
    """Split a multi-line config value into non-empty, stripped command lines."""
    return [line.strip() for line in block.splitlines() if line.strip()]


def _is_truthy(value: str) -> bool:
    """Interpret a config flag string as a boolean."""
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Configuration file
# --------------------------------------------------------------------------- #
class DMMConfig:
    """Read-only accessor over a DMM ``.ini`` configuration file.

    Keys keep their original case. A key that is present but blank is treated as
    absent, so leaving a value empty behaves exactly like omitting it; multi-line
    command blocks are non-empty once stripped and pass through unchanged.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        parser = configparser.ConfigParser()
        parser.optionxform = str          # preserve the case of keys
        with open(path, "r", encoding="utf-8") as handle:
            parser.read_file(handle)
        self.cp = parser

    def get(self, section: str, key: str, default: str = "") -> str:
        """Return a config value, or ``default`` when missing or blank."""
        try:
            value = self.cp.get(section, key)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default
        if value is None or value.strip() == "":
            return default
        return value

    def has_function(self, function: str) -> bool:
        """Return ``True`` if this config describes the named function."""
        return self.cp.has_section(function.lower())

    @property
    def name(self) -> str:
        """Human-readable DMM name (``[dmm] name``, else the file basename)."""
        return self.get("dmm", "name", os.path.basename(self.path))

    @property
    def model(self) -> str:
        """Model string (``[dmm] model``); the default identity match."""
        return self.get("dmm", "model", "")


# --------------------------------------------------------------------------- #
# Live instrument session
# --------------------------------------------------------------------------- #
class DMM:
    """A live multimeter session described entirely by a :class:`DMMConfig`.

    Interface, address and resource may be overridden at construction time, for
    example to adopt a resource string discovered by :func:`scan_bus`. Everything
    else -- terminations, timeouts, init and per-function command blocks, trigger
    and query strings -- comes from the config file.
    """

    #: Distinguishes a real session from :class:`ManualDMM` at call sites.
    manual = False

    def __init__(self, config_path: str, interface: Optional[str] = None,
                 address: Optional[str] = None,
                 resource: Optional[str] = None) -> None:
        if pyvisa is None:
            raise RuntimeError("pyvisa is required for DMM control "
                               "(pip install pyvisa)")
        self.cfg = DMMConfig(config_path)
        self.rm = pyvisa.ResourceManager()
        self.inst = None
        self._interface = (interface or
                           self.cfg.get("connection", "interface", "GPIB")).upper()
        self._address = (address if address is not None
                         else self.cfg.get("connection", "address", ""))
        self._resource = (resource if resource is not None
                          else self.cfg.get("connection", "resource", ""))
        #: Which function block was last applied, so a repeated reading on the
        #: same function does not re-send the whole setup block.
        self._current_function: Optional[str] = None

    # ---- resource string ---------------------------------------------------- #
    def resource_string(self) -> str:
        """Build the VISA resource string from the interface/address templates."""
        if self._resource:
            return self._resource
        if self._interface == "GPIB":
            template = self.cfg.get("connection", "gpib_template",
                                    "GPIB0::{addr}::INSTR")
        elif self._interface in ("LAN", "TCPIP", "ETHERNET"):
            template = self.cfg.get("connection", "lan_template",
                                    "TCPIP0::{addr}::inst0::INSTR")
        elif self._interface == "USB":
            template = self.cfg.get("connection", "usb_template", "{addr}")
        else:
            template = "{addr}"
        return template.format(addr=self._address)

    # ---- connection --------------------------------------------------------- #
    def connect(self) -> str:
        """Open the session, apply terminations and timeout, assert REN if the
        config asks for it, and run the ``[connection] init`` block.

        Returns the VISA resource string that was opened.
        """
        resource = self.resource_string()
        self.inst = self.rm.open_resource(resource)
        self.inst.timeout = int(self.cfg.get("connection", "timeout_ms", "20000"))
        self.inst.read_termination = _decode_escapes(
            self.cfg.get("connection", "read_termination", "\\n"))
        self.inst.write_termination = _decode_escapes(
            self.cfg.get("connection", "write_termination", "\\n"))
        # Some instruments (the 3458A among them) must be in remote before they
        # accept commands at all.
        if _is_truthy(self.cfg.get("connection", "assert_ren", "false")):
            try:
                self.inst.control_ren(pyvisa.constants.RENLineOperation.asrt_address)
            except Exception:
                pass
        try:
            self.inst.clear()
        except Exception:
            pass
        time.sleep(0.3)
        self.drain_stale_responses()
        for command in _lines(self.cfg.get("connection", "init", "")):
            self.inst.write(command)
            time.sleep(0.2)
        self._current_function = None
        return resource

    def drain_stale_responses(self, limit: int = 20) -> int:
        """Read and discard anything the meter is already holding.

        A selected device clear does not always empty the output buffer, and a
        reading left there by an earlier session puts every subsequent query one
        response out of step: ``ID?`` returns the previous reading, the next
        question gets ``ID?``'s answer, and so on. Because the I/O timeout is
        derived from the integration time it can be minutes long, so the symptom
        is not an error but a very long silence that looks exactly like a dead
        instrument.

        Measured 29 July 2026: a 3458A left holding one 10 V reading from the
        night before stalled a run at the meter-selection prompt for eight
        minutes before it was killed. ``NDIG?`` answering ``1.002063350E+01``
        was what gave it away.

        Returns how many stale responses were discarded.
        """
        if self.inst is None:
            return 0
        previous = self.inst.timeout
        self.inst.timeout = 800
        drained = 0
        deadline = time.time() + 3.0
        try:
            for _ in range(limit):
                try:
                    self.inst.read()
                    drained += 1
                except Exception:
                    break
                # A meter left free-running answers every read, so the count
                # limit is not a limit. Stop on the clock instead.
                if time.time() >= deadline:
                    break
        finally:
            self.inst.timeout = previous
        return drained

    # ---- read timeout ------------------------------------------------------- #
    def base_timeout_ms(self) -> int:
        """The config's ``timeout_ms``, which acts as a floor."""
        try:
            return int(float(self.cfg.get("connection", "timeout_ms", "20000")))
        except (TypeError, ValueError):
            return 20000

    def timeout_for_nplc(self, function: str, nplc: float) -> int:
        """Return the I/O timeout in ms that an ``nplc``-long reading needs.

        Never less than the config's ``timeout_ms``, so a meter that is slow
        for reasons unrelated to integration keeps whatever allowance its
        config already declared. See the notes at the top of this module for
        where the multiplier comes from.
        """
        def _number(section: str, key: str, fallback: float) -> float:
            raw = str(self.cfg.get(section, key, "")).strip()
            if not raw:
                return fallback
            try:
                value = float(raw)
            except ValueError:
                return fallback
            return value if value > 0 else fallback

        line_hz = _number("connection", "line_freq_hz", DEFAULT_LINE_FREQ_HZ)
        apertures = _number(function, "nplc_apertures", DEFAULT_NPLC_APERTURES)
        overhead = _number(function, "read_overhead_s", DEFAULT_READ_OVERHEAD_S)
        settle = _number(function, "settle_s", 0.5)

        try:
            aperture_s = float(nplc) / line_hz
        except (TypeError, ValueError, ZeroDivisionError):
            aperture_s = 0.0

        needed_ms = int((aperture_s * apertures + overhead + settle) * 1000)
        return min(max(self.base_timeout_ms(), needed_ms), TIMEOUT_CEILING_MS)

    def identify(self) -> str:
        """Return the meter's identity string, or an empty string if it has none."""
        query = self.cfg.get("identify", "query", "*IDN?")
        try:
            return self.inst.query(query).strip()
        except Exception:
            return ""

    def _drain(self, budget_s: float = 2.0) -> None:
        """Read and discard buffered or free-running output.

        Bounded by wall clock as well as by count. The count alone is not a
        bound: against a *free-running* meter every read succeeds, so the loop
        runs to its limit rather than stopping at a timeout, and forty readings
        at 10 PLC is eight seconds of doing nothing. That is exactly the state
        the restore block leaves a 3458A in, which is how draining after a
        restore came to cost more than the restore.
        """
        previous = self.inst.timeout
        self.inst.timeout = 400
        deadline = time.time() + budget_s
        try:
            for _ in range(40):
                try:
                    self.inst.read()
                except Exception:
                    break
                if time.time() >= deadline:
                    break
        finally:
            self.inst.timeout = previous

    def _parse(self, text: str) -> float:
        """Extract the first floating-point number from an instrument response."""
        match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text)
        if not match:
            raise ValueError("no number in DMM response: %r" % text)
        return float(match.group(0))

    # ---- measurement -------------------------------------------------------- #
    def resolve_nplc(self, function: str, requested: float) -> float:
        """Return an integration time this meter will actually accept.

        Callers ask for the integration they want; meters differ sharply in what
        they allow. Most accept only a discrete set - the 34401A takes
        0.02, 0.2, 1, 10 or 100 and nothing between - and several cap far lower
        than the caller might assume: the Keithley 2001 and 2002 stop at 10
        power-line cycles. Sending an unsupported value produces a settings-
        conflict error and, on some meters, silently leaves the previous
        integration in force, which would corrupt a measurement without any
        outward sign.

        A config declares its capability with either::

            nplc_values = 0.02,0.2,1,10,100     the exact allowed set
            nplc_max    = 10                    a simple ceiling

        ``nplc_values`` takes precedence. The requested value is reduced to the
        largest allowed value not exceeding it, so a caller always gets at least
        as fast a reading as it asked for and never an invalid setting. If the
        request is below everything on offer, the smallest allowed value is
        used. With neither key declared the request passes through unchanged.
        """
        allowed = self.cfg.get(function, "nplc_values", "")
        if allowed:
            try:
                values = sorted(float(v) for v in allowed.replace(";", ",").split(",")
                                if v.strip())
            except ValueError:
                values = []
            if values:
                usable = [v for v in values if v <= requested]
                return usable[-1] if usable else values[0]
        ceiling = self.cfg.get(function, "nplc_max", "")
        if ceiling:
            try:
                return min(requested, float(ceiling))
            except ValueError:
                pass
        return requested

    def max_input(self, function: str) -> Optional[float]:
        """Return the largest input this meter accepts on ``function``, or ``None``.

        Declared by ``[<function>] max_input`` in SI base units. It exists for
        safety rather than accuracy: the 34420A is an excellent choice on the
        R6161's millivolt ranges but its highest range is 100 V, and the
        generator's 1000 V range would be applied to its terminals if nothing
        stopped it. An engine is expected to skip any point that exceeds this.
        """
        value = self.cfg.get(function.lower(), "max_input", "")
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    def setup(self, function: str, dmm_range: str = "AUTO",
              nplc: Optional[float] = None) -> None:
        """Apply the ``[<function>] setup`` block.

        Commands may reference ``{range}``, ``{nplc}`` and ``{ndig}``. The block
        is re-sent whenever the range or integration time changes, because on
        most meters the range is part of the function command itself. The
        requested integration is passed through :meth:`resolve_nplc` first.

        ``{ndig}`` is the displayed-resolution setting some meters expose
        separately from the integration time -- ``NDIG`` on the 3458A -- and
        comes from that function's ``ndig`` key. Like ``nplc`` it is a property
        of the meter rather than of the run, so it lives with the meter's own
        description and not in the caller. A config that has no such command
        simply never references ``{ndig}``. On the 3458A the two were measured
        to be independent: setting ``NDIG`` does not move ``NPLC`` and vice
        versa, in either order, so the block may send them in whichever order
        reads best.
        """
        function = function.lower()
        if function not in FUNCTIONS:
            raise ValueError("unknown DMM function %r" % function)
        if not self.cfg.has_function(function):
            raise RuntimeError("%s does not describe a [%s] section; this meter "
                               "cannot make that measurement with the supplied "
                               "config" % (os.path.basename(self.cfg.path), function))
        if nplc is None:
            nplc = float(self.cfg.get(function, "nplc", "10"))
        nplc = self.resolve_nplc(function, float(nplc))

        # Scale the I/O timeout to the integration time now in use. Done here
        # rather than at connect() because connect() does not yet know which
        # function or integration the run will ask for, and a long-integrating
        # reading would otherwise time out against whatever fixed value the
        # config happened to declare.
        self.inst.timeout = self.timeout_for_nplc(function, nplc)
        # Render an integral value without a trailing '.0'; some pre-SCPI
        # parsers reject "10.0" where they accept "10".
        nplc_text = ("%g" % nplc)

        # The displayed-resolution setting, for the configs that reference it.
        # A config that writes {ndig} into its setup block and then forgets to
        # declare `ndig` would send a bare "NDIG" and get a syntax error, so
        # that combination is refused with a message naming the file rather
        # than left to fail on the wire.
        ndig_text = str(self.cfg.get(function, "ndig", "")).strip()
        if not ndig_text and "{ndig}" in self.cfg.get(function, "setup", ""):
            raise RuntimeError("%s [%s] uses {ndig} but declares no ndig value"
                               % (os.path.basename(self.cfg.path), function))

        # Autoranging is expressed differently on every meter and is rarely a
        # value the range parameter accepts: the 34401A documents its range
        # parameter as {<range>|MIN|MAX|DEF} with no AUTO among them, while the
        # Keithley dialect needs a separate :RANG:AUTO ON. A config may
        # therefore supply a distinct ``setup_auto`` block, used in place of
        # ``setup`` when the caller asks for AUTO. Without one the ordinary
        # block is used with the literal word substituted, which is correct for
        # the meters that do accept it.
        block = self.cfg.get(function, "setup", "")
        if str(dmm_range).strip().upper() == "AUTO":
            block = self.cfg.get(function, "setup_auto", "") or block

        dwell = float(self.cfg.get(function, "setup_dwell_s", "0.4"))
        for command in _lines(block):
            self.inst.write(command.format(range=dmm_range, nplc=nplc_text,
                                           ndig=ndig_text))
            time.sleep(dwell)
        if _is_truthy(self.cfg.get(function, "drain_after_setup", "false")):
            self._drain()
        self._current_function = function

    def read_one(self, function: str) -> float:
        """Take one reading using the function's trigger/query model.

        Handles the three common shapes: a single trigger-and-read query, a
        separate write-trigger followed by a read, and a plain read of a
        free-running instrument.
        """
        function = function.lower()
        if _is_truthy(self.cfg.get(function, "drain_before_read", "false")):
            self._drain()
        trigger = self.cfg.get(function, "trigger", "").strip()
        query = self.cfg.get(function, "query", "").strip()
        settle = float(self.cfg.get(function, "settle_s", "0.5"))
        if trigger and not query:
            response = self.inst.query(trigger)
            time.sleep(settle)
            return self._parse(response)
        if trigger:
            self.inst.write(trigger)
        time.sleep(settle)
        response = self.inst.query(query) if query else self.inst.read()
        return self._parse(response)

    def measure(self, function: str, dmm_range: str = "AUTO",
                nplc: Optional[float] = None, samples: int = 2,
                configure: bool = True) -> Tuple[float, float]:
        """Configure, take ``samples`` readings, and return ``(mean, spread)``.

        ``spread`` is the difference between the largest and smallest reading and
        is what the engines use as a stability check. Two samples is the default
        because the engines compare the spread against a tolerance rather than
        averaging noise away; more samples buy little on a settled DC source.

        ``configure=False`` skips the setup block, for callers that have already
        called :meth:`setup` themselves. That matters during an adjustment: the
        meter should be put on the right range **before** the source is
        energised, not after, so that the signal never arrives at a meter still
        ranged for the previous point. The engines therefore configure, energise,
        settle, and then measure without configuring again.
        """
        if configure:
            self.setup(function, dmm_range, nplc)
        values = [self.read_one(function) for _ in range(max(1, samples))]
        return sum(values) / len(values), (max(values) - min(values))

    def measure_resistance_4w(self, nominal_ohm: float = 10.0) -> float:
        """Measure a standard resistor 4-wire, for the current-limit step.

        The current-limit coefficient is derived from a voltage drop divided by
        this resistance, so a 10 milliohm error in a nominally 10 ohm standard is
        1000 ppm on that coefficient. Requires an ``[ohmf]`` section.
        """
        mean, _ = self.measure("ohmf", dmm_range="AUTO", samples=3)
        return mean

    def restore(self) -> bool:
        """Send the ``[connection] restore`` block, if the config has one.

        A run leaves the meter parked: on the 3458A, ``TARM HOLD`` on a fixed
        range with a long integration, which looks like a dead instrument to
        the next person at the bench. The restore block puts it back somewhere
        ordinary -- free-running, autoranging, displaying. It is a courtesy to
        the operator rather than part of the measurement, so a config without
        one is not an error.

        Returns whether anything was sent. Never raises: failing to tidy up
        must not turn a completed run into a failed one.
        """
        block = self.cfg.get("connection", "restore", "")
        if not block.strip() or self.inst is None:
            return False
        try:
            self.inst.timeout = max(self.base_timeout_ms(), 10000)
            for command in _lines(block):
                self.inst.write(command)
                time.sleep(0.3)
            self._drain()
            self._current_function = None
            return True
        except Exception:
            return False

    def close(self) -> None:
        """Close the VISA session; never raises.

        ``inst`` is cleared as well as closed, because a closed session that
        still looks open is worse than no session: the manager holds these open
        across a whole run and decides whether to reuse one by asking whether it
        has an ``inst``.
        """
        try:
            if self.inst is not None:
                self.inst.close()
        except Exception:
            pass
        self.inst = None


# --------------------------------------------------------------------------- #
# Manual entry fallback
# --------------------------------------------------------------------------- #
class ManualDMM:
    """Keyboard entry standing in for a remote-controlled meter.

    Implements the slice of the :class:`DMM` interface the engines use, so a
    meter with no GPIB interface -- or a bench where the reference is read by
    eye -- can still drive a full verification or adjustment. ``manual`` is the
    flag call sites check to decide whether to prompt.
    """

    manual = True

    class _Cfg:
        """Minimal stand-in so ``dmm.cfg.name`` works uniformly."""
        name = "Manual entry"
        model = "-"
        path = "-"

    def __init__(self, console, unit_hint: str = "") -> None:
        self.cfg = self._Cfg()
        self.console = console
        self.unit_hint = unit_hint
        self._function = "dcv"

    def connect(self) -> str:
        return "manual"

    def identify(self) -> str:
        return "Manual entry"

    def resolve_nplc(self, function: str, requested: float) -> float:
        """A hand-entered reading has no integration time; echo the request."""
        return requested

    def max_input(self, function: str) -> Optional[float]:
        """No declared ceiling: the operator decides what is safe to connect."""
        return None

    def setup(self, function: str, dmm_range: str = "AUTO",
              nplc: Optional[float] = None) -> None:
        self._function = function.lower()

    def read_one(self, function: str) -> float:
        unit = {"dcv": "V", "dci": "A", "ohmf": "ohm"}.get(function.lower(), "")
        return self.console.ask_float("    Enter the measured value (%s): " % unit)

    def measure(self, function: str, dmm_range: str = "AUTO",
                nplc: Optional[float] = None, samples: int = 2,
                configure: bool = True) -> Tuple[float, float]:
        """Prompt once and report a zero spread.

        A hand-entered reading has no meaningful spread, so the stability check
        upstream always passes; the operator is the stability check. ``configure``
        is accepted so the engines can call this exactly as they call the real
        driver, and ignored because there is nothing to configure.
        """
        self.setup(function, dmm_range, nplc)
        return self.read_one(function), 0.0

    def measure_resistance_4w(self, nominal_ohm: float = 10.0) -> float:
        return self.console.ask_float(
            "    Enter the measured resistance (ohm) [%g]: " % nominal_ohm,
            default=nominal_ohm)

    def restore(self) -> bool:
        """Nothing to restore on a meter driven by hand."""
        return False

    def close(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# Config discovery
# --------------------------------------------------------------------------- #
# Names of config files that failed to load, with the reason.  Appended to by
# :func:`match_config`; the menus read it so a damaged file is reported to the
# user instead of silently disappearing from the meter list.
CONFIG_ERRORS: List[str] = []


def configs_dir() -> str:
    """Return the ``dmm_configs`` folder next to this module, creating it if it
    does not exist so a fresh or relocated copy still has the folder."""
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dmm_configs")
    os.makedirs(folder, exist_ok=True)
    return folder


def list_configs(folder: Optional[str] = None) -> List[str]:
    """Return the selectable DMM config paths.

    ``*.ini`` files are offered as choices; names beginning with ``_`` are
    treated as templates or examples and skipped.
    """
    folder = folder or configs_dir()
    if not os.path.isdir(folder):
        return []
    return sorted(os.path.join(folder, name) for name in os.listdir(folder)
                  if name.lower().endswith(".ini") and not name.startswith("_"))


# --------------------------------------------------------------------------- #
# Bus scan and identification
# --------------------------------------------------------------------------- #
def _looks_like_reading(text: str) -> bool:
    """True if ``text`` looks like a free-running measurement rather than an
    identity, so a scan does not mistake a streamed reading for an ID.

    A measurement is a floating-point number written with a decimal point and/or
    an exponent. A bare integer is *not* treated as a reading, because some
    meters answer an identify query with a numeric model number (the HP 3457A
    answers ``ID?`` with ``3457``). Non-numeric strings are never readings.
    """
    text = text.strip()
    if not text:
        return False
    try:
        float(text)
    except (ValueError, TypeError):
        return False                       # not numeric at all -> an identity
    return ("." in text) or ("e" in text.lower())


def _is_present(inst) -> bool:
    """Is anything actually at this address? Decided by serial poll.

    ``list_resources()`` returns addresses the VISA layer knows about, not
    addresses that have an instrument on them, so a scan spends most of its time
    talking to empty slots. Measured on 29 July 2026:

    ==========================  =============  =============
    Operation                   present        absent
    ==========================  =============  =============
    ``read_stb()``              0.00 s         2.04 s
    ``query("*IDN?")``          0.01 - 2.14 s  2.01 s
    ==========================  =============  =============

    An absent address costs the same fixed two seconds whichever is used -- and
    that two seconds is imposed by the GPIB driver, not by the VISA timeout;
    setting ``timeout`` to 250 ms rather than 1500 ms changed a five-address
    scan not at all, 33.6 s either way. What *can* be changed is how many of
    those two-second waits are spent per address: one serial poll instead of an
    identify query for every entry in ``queries``.

    A serial poll is a bus-level operation. It sends no command, so it cannot
    provoke an error on an instrument that would object to one -- which on this
    bench is the whole point.
    """
    try:
        inst.read_stb()
        return True
    except Exception:
        return False


def _scan_sacrifice(inst) -> None:
    """Absorb the post-write corruption before probing an instrument.

    Asserting REN and sending a selected device clear are both write-like bus
    operations, and on the Advantest R6161 the command that *immediately
    follows* one loses its leading character. ``*IDN?`` then arrives as
    ``IDN?``, which its parser reads as the current-range command ``I`` with a
    range value of ``DN?`` -- an invalid X for ``IX``, which is Err.G07. The
    query gets no answer and the instrument is left showing a fault it did
    nothing to deserve.

    Measured against the R6161 on the bench, five fresh sessions per row,
    status byte polled after each:

    ==========================================  ==============  ==========
    Sequence                                    Identified      Status
    ==========================================  ==============  ==========
    bare ``*IDN?``, nothing before it           5/5             clean
    serial poll, then ``*IDN?``                 5/5             clean
    REN, then ``*IDN?``                         1/5             0x42, error
    **device clear**, then ``*IDN?``            0/5             0x42, error
    REN + clear, then ``*IDN?``                 0/5             0x42, error
    REN + clear + **bare read**, ``*IDN?``      5/5             clean
    ==========================================  ==============  ==========

    Two things follow, and the second matters more than the first. The
    sacrificial read does work -- the bottom row is as clean as the top. But the
    top row is the one to aim for: an instrument that answers a plain ``*IDN?``
    never needed the device clear that broke it, which is why :func:`scan_bus`
    now asks plainly first and only escalates to this if that draws a blank.

    A bare read is used rather than a query because it sends no command and so
    can raise nothing itself. On an instrument with nothing to say it times out
    harmlessly in a quarter of a second; on a free-running meter it consumes one
    reading, which during a scan costs nothing. This is the same trick
    :meth:`r6161.R6161._sacrifice` uses, applied where the scanner needs it.
    """
    try:
        previous = inst.timeout
    except Exception:
        return
    try:
        inst.timeout = 250
        try:
            inst.read()
        except Exception:
            pass
    finally:
        try:
            inst.timeout = previous
        except Exception:
            pass


#: Recovery commands for instruments that latch an error when probed with the
#: SCPI ``*IDN?`` they do not implement. Keyed by a substring of the response to
#: the fallback identify query.
_SCAN_RECOVERY: Dict[str, List[str]] = {
    "3458": ["RESET"],
    "3457": ["RESET"],
}


def _ask_identity(inst, query: str) -> Optional[str]:
    """Send one identify query and return a usable answer, or ``None``.

    A reading is not an answer: a free-running meter returns its display when
    addressed to talk, and so does the R6161 when a query never reached it.
    """
    try:
        response = inst.query(query).strip()
    except Exception:
        return None
    if response and not _looks_like_reading(response):
        return response
    return None


def scan_bus(timeout_ms: int = 1500, skip: Sequence[str] = (),
             queries: Sequence[str] = ("*IDN?", "ID?")) -> List[Tuple[str, Optional[str]]]:
    """Enumerate every present VISA instrument and try to identify each.

    Returns a list of ``(resource_string, id_response_or_None)``.

    Probing is done in two stages, gentlest first, and the order is the whole
    point of this function.

    **Stage one asks plainly.** A bare ``*IDN?`` on a freshly opened session,
    with no REN, no device clear and no sacrificial read -- which is exactly
    what an interactive tool like Keysight Connection Expert does when someone
    types the query by hand, and it is why doing it by hand always worked while
    the scanner provoked errors. Any SCPI instrument answers here, the R6161
    included (5/5, status clean; see :func:`_scan_sacrifice` for the table).

    **Stage two escalates, and only for what stage one could not identify.**
    REN, because a pre-SCPI meter left free-running ignores a query and keeps
    talking over it; a device clear, to flush what it has already queued; the
    sacrificial read; then each query in turn, ``ID?`` included.

    The escalation is what used to run against every instrument on the bus. It
    is intrusive -- a selected device clear aborts whatever a meter is doing --
    and on the R6161 it was actively harmful: the device clear alone made
    ``*IDN?`` fail 5 times out of 5 and lit Err.G07 on the panel each time.
    Nothing that answers a plain question is asked a rude one any more.

    ``skip`` is a collection of resource strings not to probe. It is no longer
    needed to protect the R6161 from the scan; what it is still for is avoiding
    a second concurrent session to an instrument this process already has open.
    """
    if pyvisa is None:
        return []
    skip_set = {str(item).upper() for item in skip}
    try:
        rm = pyvisa.ResourceManager()
        resources = list(rm.list_resources())
    except Exception:
        return []

    found: List[Tuple[str, Optional[str]]] = []
    for resource in resources:
        if resource.upper() in skip_set:
            continue
        identity: Optional[str] = None
        try:
            inst = rm.open_resource(resource)
        except Exception:
            found.append((resource, None))
            continue
        try:
            inst.timeout = timeout_ms
            inst.read_termination = "\n"
            inst.write_termination = "\n"
            if not _is_present(inst):
                found.append((resource, None))
                continue

            # Stage one: ask plainly. Nothing has been written to this
            # instrument, so nothing can have corrupted the query, and nothing
            # rude has been done to it if it is midway through a measurement.
            winner = -1
            if queries:
                identity = _ask_identity(inst, queries[0])
                if identity:
                    winner = 0

            if identity is None:
                # Stage two, for what did not answer. Without REN a pre-SCPI
                # meter ignores the identify query and keeps free-running, so
                # the scan reads back a bare measurement instead of an identity.
                # REN is a bus-global line; failures are non-fatal.
                try:
                    inst.control_ren(
                        pyvisa.constants.RENLineOperation.asrt_address)
                except Exception:
                    pass
                for index, query in enumerate(queries):
                    try:
                        inst.clear()
                    except Exception:
                        pass
                    time.sleep(0.05)
                    _scan_sacrifice(inst)
                    identity = _ask_identity(inst, query)
                    if identity:
                        winner = index
                        break
            # If the standard probe was rejected and a later query succeeded, the
            # instrument latched an error; clear it.
            if identity and winner > 0:
                upper = identity.upper()
                for key, commands in _SCAN_RECOVERY.items():
                    if key in upper:
                        for command in commands:
                            try:
                                inst.write(command)
                                time.sleep(0.3)
                            except Exception:
                                pass
                        break
        except Exception:
            pass
        finally:
            try:
                inst.close()
            except Exception:
                pass
        found.append((resource, identity))
    return found


def match_config(config_path: str,
                 scan_results: Sequence[Tuple[str, Optional[str]]]
                 ) -> Optional[Tuple[str, Optional[str]]]:
    """Return the scan hit whose identity matches this config, or ``None``.

    The match string comes from ``[identify] match``, defaulting to
    ``[dmm] model``, and is compared case-insensitively as a substring.

    A config file that cannot be loaded is reported once on
    :data:`CONFIG_ERRORS` and then treated as "no match", so one damaged or
    hand-edited file cannot stop the bus scan from finding the other meters.
    """
    try:
        cfg = DMMConfig(config_path)
    except Exception as exc:                      # noqa: BLE001 - see docstring
        name = os.path.basename(config_path)
        message = "%s: %s" % (name, exc)
        if message not in CONFIG_ERRORS:
            CONFIG_ERRORS.append(message)
        return None
    needle = (cfg.get("identify", "match", "") or cfg.model).strip().upper()
    if not needle:
        return None
    for resource, identity in scan_results:
        if identity and needle in identity.upper():
            return (resource, identity)
    return None
