"""Lightweight JSON-catalog internationalisation for the Advantest R6161 service tool.

UI and report translations are kept in **separate** folders so a language can be
translated for one without the other::

    lang/ui/<code>.json       console / user-interface strings
    lang/report/<code>.json   calibration-certificate strings

Drop a new file into the relevant folder to add a language there. Each file has
the form::

    {
      "_meta":   { "name": "English", "cjk": false },
      "strings": { "<key>": "<translated text>", ... }
    }

UI files (``lang/ui/``)
    The English source text *is* the key. Wrap a user-facing string with
    ``t("...")``; :func:`t` returns the translation for the current UI language if
    present and falls back to the English source text otherwise, so the UI works
    even with a missing or partial file. Set the language with
    :func:`set_ui_language`.

Report files (``lang/report/``)
    The certificate generator uses short keys (``title``, ``cert_no``, ...). A
    report language must provide a complete ``"strings"`` section.
    ``_meta.cjk`` tells the certificate generator to use the embedded CJK font.

This module keeps no third-party dependencies and is safe to import from any of
the service-tool modules. All catalogs are cached after first load; call
:func:`reload` to pick up files added or edited at run time.
"""

from __future__ import annotations

import os
import json
from typing import Dict, List, Tuple

__all__ = [
    "lang_dir",
    "reload",
    "available_ui_languages",
    "available_report_languages",
    "language_name",
    "set_ui_language",
    "ui_language",
    "t",
    "report_catalog",
    "is_cjk",
]

# --------------------------------------------------------------------------- #
# Module state
# --------------------------------------------------------------------------- #
#: Absolute path of the ``lang/`` folder that sits next to this module.
_DIR: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lang")

#: Cache of loaded catalogs, keyed by ``(kind, code)`` -> parsed-JSON ``dict``.
_cache: Dict[Tuple[str, str], dict] = {}

#: Currently selected user-interface language code (English by default).
_ui_lang: str = "en"

# A catalog "kind" is the sub-folder name under ``lang/``.
_KIND_UI = "ui"
_KIND_REPORT = "report"


# --------------------------------------------------------------------------- #
# Folder / catalog loading helpers (internal)
# --------------------------------------------------------------------------- #
def _subdir(kind: str) -> str:
    """Return ``lang/<kind>/``, creating it if it does not yet exist."""
    d = os.path.join(_DIR, kind)
    os.makedirs(d, exist_ok=True)
    return d


def _load(kind: str, code: str) -> dict:
    """Load and cache the parsed JSON catalog for ``(kind, code)``.

    A missing or unparseable file yields an empty dict (cached), so callers can
    always index into the result and rely on the English fallback.
    """
    key = (kind, str(code))
    if key in _cache:
        return _cache[key]
    catalog: dict = {}
    try:
        path = os.path.join(_subdir(kind), str(code) + ".json")
        with open(path, encoding="utf-8") as fh:
            catalog = json.load(fh)
    except Exception:
        catalog = {}
    _cache[key] = catalog
    return catalog


def _available(kind: str) -> List[str]:
    """Return the sorted language codes present in ``lang/<kind>/``.

    Files whose name starts with ``_`` are treated as templates/examples and are
    excluded.
    """
    folder = _subdir(kind)
    return sorted(
        os.path.splitext(name)[0]
        for name in os.listdir(folder)
        if name.lower().endswith(".json") and not name.startswith("_")
    )


# --------------------------------------------------------------------------- #
# Public folder / discovery API
# --------------------------------------------------------------------------- #
def lang_dir() -> str:
    """Create (if needed) and return the ``lang/`` folder.

    Ensures both the ``ui/`` and ``report/`` sub-folders exist so a fresh or
    relocated copy of the scripts always has somewhere to place catalogs.
    """
    os.makedirs(_DIR, exist_ok=True)
    _subdir(_KIND_UI)
    _subdir(_KIND_REPORT)
    return _DIR


def reload() -> None:
    """Drop the catalog cache so newly added / edited language files are used."""
    _cache.clear()


def available_ui_languages() -> List[str]:
    """Return the UI language codes available in ``lang/ui/``."""
    return _available(_KIND_UI)


def available_report_languages() -> List[str]:
    """Return the report language codes available in ``lang/report/``."""
    return _available(_KIND_REPORT)


def language_name(code: str) -> str:
    """Return the human-readable name for a language code.

    The name is taken from ``_meta.name`` in the report catalog first, then the
    UI catalog; if neither provides one the code itself is returned.
    """
    for kind in (_KIND_REPORT, _KIND_UI):
        name = _load(kind, code).get("_meta", {}).get("name")
        if name:
            return name
    return str(code)


# --------------------------------------------------------------------------- #
# UI translation  (lang/ui/<code>.json)
# --------------------------------------------------------------------------- #
def set_ui_language(code: str) -> None:
    """Select the active UI language and warm its catalog cache."""
    global _ui_lang
    _ui_lang = str(code)
    _load(_KIND_UI, _ui_lang)


def ui_language() -> str:
    """Return the currently selected UI language code."""
    return _ui_lang


def t(text: str, **fmt: object) -> str:
    """Translate a UI string.

    The English source text is the lookup key; if the active language has no
    entry the English text is returned unchanged. Any keyword arguments are
    applied with :meth:`str.format`; a formatting error leaves the string as-is
    rather than raising, so a malformed catalog can never crash the UI.
    """
    s = _load(_KIND_UI, _ui_lang).get("strings", {}).get(text, text)
    if fmt:
        try:
            s = s.format(**fmt)
        except Exception:
            pass
    return s


# --------------------------------------------------------------------------- #
# Report catalogs  (lang/report/<code>.json)
# --------------------------------------------------------------------------- #
def report_catalog(code: str):
    """Return the report string dict for a language, or ``None`` if unavailable."""
    return _load(_KIND_REPORT, code).get("strings") or None


def is_cjk(code: str) -> bool:
    """Return ``True`` if this report language needs the embedded CJK font."""
    return bool(_load(_KIND_REPORT, code).get("_meta", {}).get("cjk", False))
