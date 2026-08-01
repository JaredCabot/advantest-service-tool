"""Measurement uncertainty budget for the R6161 verification certificate.

The tool does not and cannot know the uncertainty of your reference standards.
What it can do is hold your evaluated budget, apply it to every point at the
point's own reading and range, and let the certificate state it -- which is what
turns a set of measurements into something issuable under an accreditation.

The budget lives in ``uncertainty.ini`` beside ``report_config.ini``. It is
optional: with the file absent or empty, every row's uncertainty is ``None``, the
certificate's uncertainty column reads ``-``, and the certificate says plainly
that no budget is reported. That is the behaviour this module was added to make
optional rather than mandatory.

Two forms of input, per function and optionally per range
--------------------------------------------------------
A section may give its figures as **standard** uncertainties (k = 1), which are
combined and then multiplied by the coverage factor::

    ppm_reading = 1.5      ppm of the reading
    ppm_range   = 0.3      ppm of the range's full scale
    floor_uv    = 0.2      an absolute floor, in microvolts (nanoamps: floor_na)

or as figures that are **already expanded**, which are combined and used as they
stand -- the form a calibration certificate for the reference meter usually
quotes ("+/- 3 ppm of reading, k = 2")::

    expanded    = yes
    ppm_reading = 3.0

Either way the same three keys are read, so a budget can be written in whichever
form the paperwork you are copying from happens to use. ``expanded`` says which
it is; nothing else changes.

How the terms combine
---------------------
``combine`` decides, because it depends on what the figures are.

``rss`` (the default)
    Root sum of squares, which assumes the three terms are independent
    contributions to one budget::

        u = sqrt( (ppm_reading * reading)^2 + (ppm_range * fs)^2 + floor^2 )

``linear``
    Arithmetic sum, which is how the two halves of a *specification limit* are
    meant to be read: "20 ppm of reading + 5 ppm of range" is a single limit
    whose parts are added, not two independent uncertainties::

        u = ppm_reading * reading + ppm_range * fs + floor

    A figure copied from a data sheet almost always wants this. Combining a
    spec's own terms in quadrature understates it -- for a 3458A's 100 mA range
    it gives 36 ppm where the specification says 41 -- and under guarded
    acceptance an understated uncertainty flatters the result.

The observed spread joins in quadrature either way::

    U = k * sqrt( u^2 + s^2 )

``s`` is the spread between the two readings the tool takes at every point -- a
type-A contribution it measures for free, included when
``include_observed_spread`` is set. It is genuinely independent of the
reference's specification, so quadrature is right for it even where the
specification's own terms are summed.

A voltage-drop point adds its standard resistor's ppm to ``ppm_reading``, because
I = V / R makes a relative error in R a relative error in the current, and
divides ``floor_uv`` by R, because a microvolt floor at the meter is that many
microvolts across the resistor.

The decision rule
-----------------
``decision_rule`` decides whether the verdict takes any notice of all this:

``simple``
    ILAC-G8 4.2.1 simple acceptance. The verdict stays |deviation| <=
    specification and the uncertainty is reported beside it for the reader to
    weigh. Nothing that passes on the specification alone starts failing.
``guarded``
    |deviation| <= specification - U. A point too close to the limit to call
    becomes a fail. Stricter, and it bites hardest where the reference is
    weakest -- on 100 mA measured directly a 3458A is only about 1.3:1 better
    than the R6161's own specification, so U eats a real fraction of the band.

Both are legitimate; neither is the tool's to choose, which is why it is a
setting and not a constant.
"""

from __future__ import annotations

import configparser
import math
import os
from typing import Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))

#: Filename, beside this module.
FILENAME = "uncertainty.ini"

#: Coverage factor used when the file does not say. k = 2 is about 95 % for a
#: normal distribution and is what the certificate's standard statement
#: describes.
DEFAULT_K = 2.0

#: The functions a budget can be written for. ``divider`` and ``shunt`` are
#: separate from ``dcv`` and ``dci`` because they are different measurements,
#: often on a different meter: the divider ranges may be read by a nanovolt
#: meter, and a voltage-drop current point is read as volts across a standard.
FUNCTIONS = ("dcv", "divider", "dci", "shunt")

#: Keys read from a function section. ``floor_uv`` and ``floor_na`` are the same
#: term in the unit that suits the function; both are converted to SI here.
_FLOOR_SCALE = {"floor_uv": 1e-6, "floor_na": 1e-9, "floor_si": 1.0}

DECISION_RULES = ("simple", "guarded")

#: How a section's own terms are combined. See the module docstring.
COMBINE_MODES = ("rss", "linear")


def _float(parser: configparser.ConfigParser, section: str, key: str
           ) -> Optional[float]:
    """Return a numeric config value, or ``None`` when absent or blank."""
    try:
        raw = parser.get(section, key)
    except (configparser.NoSectionError, configparser.NoOptionError):
        return None
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _flag(parser: configparser.ConfigParser, section: str, key: str,
          default: bool) -> bool:
    """Return a boolean config value, or ``default`` when absent or blank."""
    try:
        raw = parser.get(section, key)
    except (configparser.NoSectionError, configparser.NoOptionError):
        return default
    raw = (raw or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


class Budget:
    """One loaded uncertainty budget. Empty is a valid state and the default."""

    def __init__(self, sections: Optional[Dict[str, dict]] = None,
                 k: float = DEFAULT_K, decision_rule: str = "simple",
                 include_observed_spread: bool = True,
                 resistor_ppm: Optional[Dict[str, float]] = None,
                 combine: str = "rss",
                 path: str = "") -> None:
        self.sections = dict(sections or {})
        self.k = k
        self.decision_rule = (decision_rule if decision_rule in DECISION_RULES
                              else "simple")
        self.combine = combine if combine in COMBINE_MODES else "rss"
        self.include_observed_spread = include_observed_spread
        self.resistor_ppm = dict(resistor_ppm or {})
        self.path = path

    # ---- loading ----------------------------------------------------------- #
    @classmethod
    def load(cls, path: Optional[str] = None) -> "Budget":
        """Read ``uncertainty.ini``, returning an empty budget if it is not there.

        A malformed file yields an empty budget rather than an exception: an
        uncertainty statement is an addition to a certificate, and failing to
        produce the certificate at all because a comma was mistyped in an
        optional file would be the wrong trade.
        """
        path = path or os.path.join(HERE, FILENAME)
        parser = configparser.ConfigParser()
        try:
            if not parser.read(path, encoding="utf-8"):
                return cls(path=path)
        except Exception:
            return cls(path=path)

        sections: Dict[str, dict] = {}
        for name in parser.sections():
            head = name.split(".", 1)[0].strip().lower()
            if head not in FUNCTIONS:
                continue
            combine = (parser.get(name, "combine", fallback="") or "").strip().lower()
            terms = dict(
                ppm_reading=_float(parser, name, "ppm_reading") or 0.0,
                ppm_range=_float(parser, name, "ppm_range") or 0.0,
                floor_si=0.0,
                expanded=_flag(parser, name, "expanded", False),
                # Blank means "whatever [general] says", resolved at use so a
                # change there reaches every section that did not override it.
                combine=combine if combine in COMBINE_MODES else None,
            )
            for key, scale in _FLOOR_SCALE.items():
                value = _float(parser, name, key)
                if value:
                    terms["floor_si"] = value * scale
            if any((terms["ppm_reading"], terms["ppm_range"],
                    terms["floor_si"])):
                sections[name.strip().lower()] = terms

        resistor_ppm = {}
        for key in ("1mA", "10mA", "100mA"):
            value = _float(parser, "resistor", key)
            if value:
                resistor_ppm[key] = value

        return cls(sections=sections,
                   k=_float(parser, "general", "k") or DEFAULT_K,
                   decision_rule=(parser.get("general", "decision_rule",
                                             fallback="simple") or "simple")
                   .strip().lower(),
                   include_observed_spread=_flag(
                       parser, "general", "include_observed_spread", True),
                   resistor_ppm=resistor_ppm,
                   combine=(parser.get("general", "combine", fallback="rss")
                            or "rss").strip().lower(),
                   path=path)

    # ---- use --------------------------------------------------------------- #
    def is_empty(self) -> bool:
        """``True`` when no function has any terms, so nothing can be reported."""
        return not self.sections

    def _terms_for(self, function: str, range_name: str) -> Optional[dict]:
        """Return the terms for one range: its own section, else its function's."""
        return (self.sections.get("%s.%s" % (function, range_name.lower()))
                or self.sections.get(function))

    def expanded_for(self, function: str, range_name: str,
                     reading_si: Optional[float], fs_si: float,
                     spread_si: Optional[float] = None,
                     shunt_ohm: Optional[float] = None) -> Optional[float]:
        """Return the expanded uncertainty in SI units, or ``None``.

        ``None`` means "nothing to report" -- an empty budget, no terms for this
        function, or no reading to attach an uncertainty to -- and leaves the
        certificate's uncertainty column showing a dash for that row.
        """
        if reading_si is None:
            return None
        terms = self._terms_for(function, range_name)
        if terms is None:
            return None

        ppm_reading = terms["ppm_reading"]
        floor_si = terms["floor_si"]
        if shunt_ohm:
            # I = V / R. A relative error in R is a relative error in the
            # current, so the resistor's ppm joins the reading term; and the
            # meter's absolute floor is a voltage across R, so it becomes a
            # current by dividing by it.
            ppm_reading = math.hypot(ppm_reading,
                                     self.resistor_ppm.get(range_name, 0.0))
            floor_si = floor_si / shunt_ohm

        parts = [ppm_reading * 1e-6 * abs(reading_si),
                 terms["ppm_range"] * 1e-6 * abs(fs_si),
                 floor_si]
        mode = terms["combine"] or self.combine
        # A specification limit's own terms are summed; independent
        # contributions to a budget are combined in quadrature. See the module
        # docstring for why the difference matters under a guard band.
        combined = (sum(parts) if mode == "linear"
                    else math.sqrt(sum(part ** 2 for part in parts)))
        # The observed spread is independent of the reference's specification
        # whichever way that specification's own terms went together, so it
        # always joins in quadrature.
        if self.include_observed_spread and spread_si:
            combined = math.hypot(combined, float(spread_si))
        if combined <= 0:
            return None
        return combined if terms["expanded"] else combined * self.k

    def guard_band(self) -> bool:
        """``True`` when the verdict must allow for the uncertainty."""
        return self.decision_rule == "guarded"

    # ---- what the certificate says ----------------------------------------- #
    def note(self) -> str:
        """The uncertainty paragraph for the certificate, matching what was done.

        Returned rather than assembled at the call site so that the statement and
        the arithmetic cannot drift apart: a certificate claiming simple
        acceptance while the rows were guard-banded would be worse than one
        claiming nothing at all.
        """
        if self.is_empty():
            return (
                "No measurement uncertainty budget is reported. The uncertainty "
                "column is left blank because this tool does not compute one: "
                "pass/fail is assessed against the manufacturer's published "
                "accuracy specification alone, with no allowance made for the "
                "uncertainty of the reference standards. A laboratory issuing "
                "this certificate under an accreditation should evaluate and "
                "state its own uncertainty of measurement.")

        rule = (
            "Conformity is assessed against the manufacturer's published "
            "accuracy specification without allowance for the reported "
            "uncertainty (simple acceptance, ILAC-G8 section 4.2.1); the "
            "expanded uncertainty is stated alongside each result."
            if not self.guard_band() else
            "Conformity is assessed against the manufacturer's published "
            "accuracy specification reduced by the reported expanded "
            "uncertainty, so that a result too close to the limit to resolve "
            "is reported as a failure (guarded acceptance).")
        spread = (
            " The evaluation includes, per point, the observed spread between "
            "repeated readings, combined in quadrature."
            if self.include_observed_spread else "")
        combining = (
            " The contributions to each budget are summed arithmetically, as "
            "the terms of a specification limit."
            if self.combine == "linear" else
            " The contributions to each budget are combined as a root sum of "
            "squares.")
        return (
            "The reported expanded uncertainty of measurement is stated as the "
            "standard uncertainty of the measurement multiplied by a coverage "
            "factor k = %s, providing a level of confidence of approximately "
            "95 %%. It is evaluated from the laboratory's own budget for the "
            "reference standards used, applied at each point's reading and "
            "range.%s%s %s"
            % (("%g" % self.k), combining, spread, rule))
