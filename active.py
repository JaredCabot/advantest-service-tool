"""The instrument this session is working on.

One session works on exactly one instrument. That is not a simplification: the
operator has one thing cabled to the bus, the certificate names one item under
test, and a run that changed instrument half way through would produce a
document describing neither. So rather than threading four modules through every
helper in the engines -- including the ones that grade a single row and have no
business knowing what a session is -- the choice is bound once, here, and read
from here.

    import active
    active.bind(instrument)              # done by the tool at startup
    active.spec.tolerance(...)           # anywhere in the core

What is bound is a :class:`instruments.Instrument`: the four modules a plugin
provides, already conformance-checked. Nothing in the core imports a plugin's
modules directly, and nothing in the core names an instrument.

Reading any of them before something is bound raises with the name that was
reached for and what to do about it. That matters because the alternative is an
``AttributeError: 'NoneType' object has no attribute 'tolerance'`` from inside a
grading helper, which says nothing about the real mistake.
"""

from __future__ import annotations

from typing import Optional

__all__ = ["bind", "unbind", "instrument", "driver", "spec", "perf", "adjust",
           "is_bound", "require"]


class _Unbound:
    """Stands in for a plugin module until one is bound, and explains itself."""

    def __init__(self, role: str) -> None:
        self._role = role

    def __getattr__(self, name: str):
        # AttributeError, not RuntimeError: this has to behave like an ordinary
        # failed attribute lookup so that ``getattr(active.driver, x, default)``
        # returns the default instead of exploding. Callers that want the
        # explanation get it; callers that offered a fallback get the fallback.
        raise AttributeError(
            "no instrument is bound: active.%s.%s was reached first. The tool "
            "binds the instrument it opened before any procedure runs; a test "
            "or a harness that calls the engines directly must call "
            "active.bind(instrument) itself." % (self._role, name))

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "<unbound %s>" % self._role


#: The loaded plugin, or ``None``. Read it to ask what the instrument can do;
#: the four module attributes below are the shortcuts worth using everywhere
#: else.
instrument: Optional[object] = None

#: The plugin's modules. Rebound by :func:`bind`, so read them as
#: ``active.driver`` rather than importing the names -- a ``from`` import binds
#: the sentinel and never sees the real module.
driver = _Unbound("driver")
spec = _Unbound("spec")
perf = _Unbound("perf")
adjust = _Unbound("adjust")


def bind(loaded) -> None:
    """Make this loaded instrument the one the core works on."""
    global instrument, driver, spec, perf, adjust
    instrument = loaded
    driver = loaded.driver_module
    spec = loaded.spec
    perf = loaded.perf_plan
    adjust = loaded.adjust_plan if loaded.adjust_plan is not None \
        else _Unbound("adjust")


def unbind() -> None:
    """Forget the instrument. Used by tests that bind more than one."""
    global instrument, driver, spec, perf, adjust
    instrument = None
    driver = _Unbound("driver")
    spec = _Unbound("spec")
    perf = _Unbound("perf")
    adjust = _Unbound("adjust")


def is_bound() -> bool:
    """Is there an instrument to work on?"""
    return instrument is not None


def can(capability: str) -> bool:
    """Does the bound instrument claim this capability? False when unbound."""
    return bool(instrument is not None and instrument.can(capability))


def require(capability: str) -> None:
    """Raise unless the bound instrument claims this capability.

    Belt and braces: the menu only offers what a plugin declares, so this should
    never fire. It exists because "should never" is how a half-supported
    procedure reaches an energised instrument.
    """
    if instrument is None:
        raise RuntimeError("no instrument is bound")
    if not instrument.can(capability):
        raise RuntimeError("%s does not support %s"
                           % (instrument.title, capability))
