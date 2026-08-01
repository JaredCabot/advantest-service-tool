"""Bridge between this tool and the house calibration-certificate generator.

The certificate itself is rendered by :mod:`cert_report`, which is the shared
generator: it is equipment-agnostic, it fixes every aspect of the appearance --
page size, fonts, colours, column sizing, rounding, page breaks -- and it is
**copied in unmodified**. Two certificates produced months apart, for different
instruments, come out typographically identical because nothing here is allowed
to touch the layout.

What this module does is the part that is specific to the R6161 tool:

* turns a report language code into a catalog the generator understands, using
  the translations in ``lang/report/``. The generator carries English and
  Japanese itself; every other language this tool ships is handed to it as a
  complete ``catalog``, which is the documented way to add one.
* keeps the handful of result-status strings this instrument needs -- a point
  skipped because it would exceed the reference meter's input limit, or one the
  meter would not read -- which are outside the generator's own vocabulary.
* renders one certificate per requested language.

Everything else -- the identity blocks, the standards, the rows -- is assembled
by the caller against the generator's data contract, which is documented with
the generator rather than here.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

try:
    import cert_report
except Exception:                                        # pragma: no cover
    cert_report = None

try:
    import i18n
except Exception:                                        # pragma: no cover
    i18n = None


#: Result-status strings this instrument needs and the generator does not carry.
#:
#: The generator renders a row's ``status`` by looking the key up in the
#: catalog, so an entry here is all it takes to add one. Both mean "recorded but
#: not graded", which is the reason they exist: a point that could not be
#: measured belongs on the certificate as a stated absence, not as a silent gap
#: in the table and certainly not as a pass.
_STATUS_STRINGS: Dict[str, str] = {
    "overrange": "Not tested (exceeds reference limit)",
    "noread": "No reading",
}


def available_languages() -> List[str]:
    """Report language codes this installation can render."""
    if i18n is None:
        return ["en"]
    try:
        return list(i18n.available_report_languages()) or ["en"]
    except Exception:
        return ["en"]


def _catalog_for(lang: str) -> Dict[str, str]:
    """Return the catalog additions and overrides for ``lang``.

    Raises for a language with no catalog installed. Refusing is deliberate:
    emitting an English certificate under a foreign language code would
    misrepresent the document.
    """
    strings: Dict[str, str] = {}
    if i18n is not None:
        try:
            strings = dict(i18n.report_catalog(lang) or {})
        except Exception:
            strings = {}
    if not strings and str(lang).lower() not in ("en", "ja"):
        raise ValueError("no report catalog for language %r" % lang)
    merged: Dict[str, str] = dict(_STATUS_STRINGS)
    merged.update(strings)
    return merged


def is_cjk(lang: str) -> bool:
    """Does this language need the CJK font?"""
    if i18n is None:
        return str(lang).lower() == "ja"
    try:
        return bool(i18n.is_cjk(lang))
    except Exception:
        return str(lang).lower() == "ja"


def generate(data: Dict[str, Any], lang: str, out_path: str) -> str:
    """Render one certificate. ``data`` follows the generator's contract.

    The tool's own translations are passed as ``strings`` rather than as a whole
    ``catalog``, so that any key this tool has not translated falls back to the
    generator's wording instead of to a blank.
    """
    if cert_report is None:
        raise RuntimeError("cert_report is unavailable; cannot render a "
                           "certificate (pip install reportlab)")
    payload = dict(data)
    strings = dict(_catalog_for(lang))
    strings.update(payload.get("strings") or {})
    if is_cjk(lang):
        strings["_cjk"] = True
    payload["strings"] = strings
    return cert_report.generate(payload, lang, out_path)


def generate_all(data: Dict[str, Any], out_dir: str, basename: str,
                 languages: List[str]) -> List[str]:
    """Render the certificate once per language. Returns the paths written.

    A language that fails is reported and skipped rather than abandoning the
    rest: an operator who asked for six certificates and can have five is better
    served by five than by none.
    """
    os.makedirs(out_dir, exist_ok=True)
    written: List[str] = []
    for lang in (languages or ["en"]):
        path = os.path.join(out_dir, "%s_%s.pdf" % (basename, str(lang).upper()))
        try:
            written.append(generate(data, lang, path))
        except Exception as exc:
            print("   could not render the %s certificate: %s" % (lang, exc))
    return written


# The row builder and the overall verdict come straight from the generator, so
# that grading is derived from the numbers in exactly one place. A caller that
# hand-wrote a verdict could produce one its own figures contradict.
def make_row(*args, **kwargs) -> dict:
    """Build one graded results row. See :func:`cert_report.make_row`."""
    if cert_report is None:
        raise RuntimeError("cert_report is unavailable")
    return cert_report.make_row(*args, **kwargs)


def apply_guard_band(row: dict) -> dict:
    """Re-derive one row's verdict against the tolerance less its uncertainty.

    Guarded acceptance: a result whose deviation is inside the specification but
    within the expanded uncertainty of the limit cannot be shown to conform, and
    is reported as a failure.

    The verdict is still *derived*, from the same three numbers the row already
    carries, and is re-derived here rather than asserted anywhere: a row can no
    more be given a guard-banded verdict its own figures contradict than a plain
    one. A row with no uncertainty, no tolerance or no reading is left exactly as
    :func:`make_row` graded it -- there is nothing to guard-band with.
    """
    tolerance, deviation = row.get("tolerance"), row.get("deviation")
    expanded = row.get("uncertainty")
    if row.get("status") or None in (tolerance, deviation, expanded):
        return row
    band = tolerance - abs(expanded)
    # A negative band means the reference cannot resolve the specification at
    # all. Grading against it would fail the point for the reference's
    # shortcomings rather than the instrument's, so the row is left ungraded and
    # the certificate reports it as not specified.
    row["passed"] = None if band <= 0 else (abs(deviation) <= band)
    return row


def overall_verdict(rows) -> Optional[bool]:
    """Overall pass/fail for a set of rows, or ``None`` if nothing was graded."""
    if cert_report is None:
        return None
    return cert_report.overall_pass(rows)
