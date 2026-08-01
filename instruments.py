"""Instrument plugins: discovery, loading, and the contract a plugin must meet.

An instrument is a folder under ``instruments/`` holding an ``instrument.ini``
and the Python modules it names. Adding support for a new instrument means
adding a folder; nothing in the core is edited, and nothing here knows the name
of any particular instrument.

    instruments/
        r6161/
            instrument.ini      identity, capabilities, defaults
            driver.py           the instrument over GPIB
            spec.py             the published accuracy specification
            perf_plan.py        what a verification measures
            adjust_plan.py      what an adjustment sets

Two stages, deliberately
------------------------
:func:`discover` reads every ``instrument.ini`` and **imports nothing**. That is
what lets the tool list what it supports, match a bus scan against it, and
report a damaged plugin, all before any third-party code has run. A plugin is
imported only by :meth:`Plugin.load`, once the operator has chosen it.

The separation is not ceremony. A plugin is code someone else wrote, and the
same reasoning that made one unparseable ``dmm_configs`` file stop aborting the
GPIB scan applies with more force here: a plugin that will not import must cost
its own menu entry and nothing else.

The contract
------------
:func:`check_conformance` is the whole of it, and it runs at load. It is a list
of names the core will reach for, derived from what the engines actually use
rather than from an idea of what an instrument ought to provide. A plugin that
is missing one is rejected with that name in the message -- which is a better
morning than discovering it forty minutes into an adjustment.

Capabilities
------------
An instrument may support verification without adjustment: its calibration
commands may be undocumented, or known but not trusted. ``[capabilities]``
declares what the plugin can actually do, and the menu offers only that. There
is no half-supported state where a menu item exists and fails when chosen.
"""

from __future__ import annotations

import configparser
import importlib.util
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))

#: The folder holding one sub-folder per instrument.
FOLDER = "instruments"

#: The manifest inside each instrument folder.
MANIFEST = "instrument.ini"

#: What the core calls on the driver **object** returned by ``open()``. Derived
#: from the ``gen.*`` calls the engines, the menu and the procedure helpers
#: make; anything absent here is not the core's business.
REQUIRED_DRIVER_METHODS: Tuple[str, ...] = (
    "identify", "ext_cal_enabled", "poll_status", "close",
    "standby", "operate", "select_range", "set_output_on_range",
    "set_sense", "set_guard", "set_voltage_limit", "set_current_limit",
)

#: Driver attributes the certificate and the menus read. ``description`` is a
#: property on the R6161; anything that answers to the name will do.
REQUIRED_DRIVER_ATTRIBUTES: Tuple[str, ...] = (
    "idn", "model", "firmware", "serial", "options", "description", "resource",
)

#: Methods needed only for an adjustment, checked only when the plugin claims
#: that capability.
ADJUST_DRIVER_METHODS: Tuple[str, ...] = (
    "select_cal_point", "enter_value_verified", "cal_end",
    "arm_error_reporting", "verify_cal_commit",
)

#: Methods needed only to read the calibration memory.
CALRAM_DRIVER_METHODS: Tuple[str, ...] = ("read_cal_image",)

#: Module-level names the core reads off the driver MODULE, as opposed to an
#: instance of the driver class.
REQUIRED_DRIVER_MODULE: Tuple[str, ...] = (
    "RANGES", "SETTLE_S", "THERMAL_WAIT_S",
    "INITIAL_EXT_SENSE", "INITIAL_EXT_GUARD",
    "POINT_OVERHEAD_S", "HOOKUP_OVERHEAD_S",
)

CALRAM_DRIVER_MODULE: Tuple[str, ...] = (
    "CAL_BLOCK_LEN", "decode_cal_block", "compare_cal_blocks",
    "validate_cal_block",
)

#: What :mod:`perf_engine` reads from the specification module.
REQUIRED_SPEC: Tuple[str, ...] = (
    "tolerance", "INTERVAL_NAMES", "DEFAULT_INTERVAL",
    "conditions_note", "specification_note",
)

#: What :mod:`perf_engine` and :mod:`perf_plan` need of a verification plan.
REQUIRED_PERF: Tuple[str, ...] = (
    "plan", "display_value", "measured_si",
    "SAFE_VOLTAGE_LIMIT", "SAFE_CURRENT_LIMIT",
)

#: What :mod:`adjust_engine` needs of an adjustment plan.
REQUIRED_ADJUST: Tuple[str, ...] = (
    "groups", "total_points", "units", "unit_key", "entry_value",
    "check_bounds", "stability_tolerance", "will_rd_accept",
)

#: Capability names a manifest may claim. Anything else in ``[capabilities]`` is
#: reported rather than ignored, because a typo there silently removes a menu
#: item and the operator has no way to tell that from "not supported".
CAPABILITIES: Tuple[str, ...] = ("verify", "adjust", "calram")


class PluginError(Exception):
    """A plugin could not be used. The message says which plugin and why."""


def plugins_dir(folder: Optional[str] = None) -> str:
    """Return the instruments folder, creating it if a relocated copy lacks it."""
    path = folder or os.path.join(HERE, FOLDER)
    os.makedirs(path, exist_ok=True)
    return path


def _split(value: str) -> List[str]:
    """Split a comma-separated manifest value into a clean list."""
    return [item.strip() for item in (value or "").split(",") if item.strip()]


class Plugin:
    """One instrument's manifest, read but not yet imported.

    Everything here comes from ``instrument.ini``. The modules are untouched
    until :meth:`load`, so a ``Plugin`` is safe to build for a folder whose code
    is broken -- which is exactly the case that has to be reportable.
    """

    def __init__(self, path: str, config: configparser.ConfigParser) -> None:
        self.path = path
        self.folder = os.path.basename(path.rstrip(os.sep))
        self._config = config

        def get(section: str, key: str, default: str = "") -> str:
            try:
                value = config.get(section, key)
            except (configparser.NoSectionError, configparser.NoOptionError):
                return default
            return (value or "").strip() or default

        self.key = get("instrument", "key", self.folder).lower()
        self.name = get("instrument", "name", self.key.upper())
        self.manufacturer = get("instrument", "manufacturer")
        self.models = _split(get("instrument", "models"))
        self.description = get("instrument", "description")
        self.identity_match = [m.upper() for m
                               in _split(get("instrument", "identity_match"))
                               or self.models]
        self.default_resource = get("instrument", "default_resource",
                                    "GPIB0::1::INSTR")
        self.notes = get("instrument", "notes")

        self.capabilities = set()
        unknown = []
        if config.has_section("capabilities"):
            for name, value in config.items("capabilities"):
                if name.strip().lower() not in CAPABILITIES:
                    unknown.append(name)
                elif (value or "").strip().lower() in ("1", "true", "yes", "on"):
                    self.capabilities.add(name.strip().lower())
        self.unknown_capabilities = unknown

        self.modules = {
            "driver": get("modules", "driver", "driver"),
            "spec": get("modules", "spec", "spec"),
            "perf": get("modules", "perf", "perf_plan"),
            "adjust": get("modules", "adjust", "adjust_plan"),
        }
        self.driver_class = get("modules", "driver_class", "")

        #: Timing defaults from the manifest, applied over the driver module's
        #: own at load. A manifest need not carry any; the module's values are
        #: the instrument's own and are the better default.
        self.timing: Dict[str, float] = {}
        if config.has_section("timing"):
            for name, value in config.items("timing"):
                try:
                    self.timing[name.strip().upper()] = float(value)
                except (TypeError, ValueError):
                    pass

        self.loaded: Optional["Instrument"] = None

    # ---- what the menus need to know before loading ------------------------ #
    @property
    def title(self) -> str:
        """The instrument's name as a heading, maker included."""
        if self.manufacturer and not self.name.upper().startswith(
                self.manufacturer.upper()):
            return "%s %s" % (self.manufacturer, self.name)
        return self.name

    def can(self, capability: str) -> bool:
        """Does this plugin claim to be able to do this?"""
        return capability in self.capabilities

    def matches(self, identity: Optional[str]) -> bool:
        """Is this ``*IDN?`` response this instrument?"""
        if not identity:
            return False
        upper = str(identity).upper()
        return any(token in upper for token in self.identity_match)

    def problems(self) -> List[str]:
        """Everything wrong with the manifest, as plain sentences.

        Reported rather than raised: a manifest can be wrong in ways that still
        leave the plugin usable, and the operator should be told without being
        stopped.
        """
        out = []
        if not self.identity_match:
            out.append("no identity_match and no models, so a bus scan can "
                       "never recognise it")
        if not self.capabilities:
            out.append("claims no capabilities, so it would have no menu items")
        for name in self.unknown_capabilities:
            out.append("unknown capability %r (known: %s)"
                       % (name, ", ".join(CAPABILITIES)))
        for role, module in self.modules.items():
            if role == "adjust" and not self.can("adjust"):
                continue
            if not os.path.isfile(os.path.join(self.path, module + ".py")):
                out.append("%s module %r is missing" % (role, module))
        return out

    # ---- loading ----------------------------------------------------------- #
    def load(self) -> "Instrument":
        """Import this plugin's modules and check the contract. Cached.

        Raises :class:`PluginError` naming the plugin and the first thing wrong.
        """
        if self.loaded is not None:
            return self.loaded
        problems = self.problems()
        if problems:
            raise PluginError("%s: %s" % (self.key, "; ".join(problems)))

        modules = {}
        for role, name in self.modules.items():
            if role == "adjust" and not self.can("adjust"):
                modules[role] = None
                continue
            try:
                modules[role] = self._import(name)
            except Exception as exc:                      # noqa: BLE001
                raise PluginError("%s: %s.py would not import: %s"
                                  % (self.key, name, exc))

        instrument = Instrument(self, modules)
        missing = check_conformance(instrument)
        if missing:
            raise PluginError("%s: %s" % (self.key, "; ".join(missing)))
        instrument.apply_timing(self.timing)
        self.loaded = instrument
        return instrument

    def _import(self, name: str):
        """Import one module out of this plugin's folder, under its own name.

        The module name is namespaced by the plugin key so two instruments can
        both have a ``driver`` module without colliding in ``sys.modules``, and
        the folder is put on the path so a plugin's modules can import each
        other by their plain names.
        """
        path = os.path.join(self.path, name + ".py")
        qualified = "instrument_%s_%s" % (self.key, name)
        if qualified in sys.modules:
            return sys.modules[qualified]
        if self.path not in sys.path:
            sys.path.insert(0, self.path)
        spec = importlib.util.spec_from_file_location(qualified, path)
        if spec is None or spec.loader is None:
            raise ImportError("no module at %s" % path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            del sys.modules[qualified]
            raise
        return module


class Instrument:
    """A loaded plugin: its modules, and the driver class it opens with."""

    def __init__(self, plugin: Plugin, modules: Dict[str, object]) -> None:
        self.plugin = plugin
        self.key = plugin.key
        self.name = plugin.name
        self.title = plugin.title
        self.driver_module = modules["driver"]
        self.spec = modules["spec"]
        self.perf_plan = modules["perf"]
        self.adjust_plan = modules["adjust"]

    def can(self, capability: str) -> bool:
        return self.plugin.can(capability)

    @property
    def driver_class(self):
        """The class that talks to the instrument.

        Named in the manifest, or found as the only class in the driver module
        that carries the required methods -- so a plugin author who forgets to
        name it gets a working plugin rather than an error about a key.
        """
        named = self.plugin.driver_class
        if named:
            return getattr(self.driver_module, named)
        candidates = [obj for obj in vars(self.driver_module).values()
                      if isinstance(obj, type)
                      and all(hasattr(obj, m) for m in REQUIRED_DRIVER_METHODS)]
        if len(candidates) != 1:
            raise PluginError(
                "%s: name the driver class in [modules] driver_class; %d "
                "classes in %s carry the required methods"
                % (self.key, len(candidates), self.plugin.modules["driver"]))
        return candidates[0]

    def open(self, resource: str, **kwargs):
        """Open the instrument at this VISA resource."""
        return self.driver_class(resource, **kwargs)

    def apply_timing(self, timing: Dict[str, float]) -> List[str]:
        """Push manifest timing values into the driver module.

        Returns the names it could not use. The driver module's own values are
        the instrument's defaults and are kept where the manifest is silent or
        wrong -- silently substituting a timing value for a run that produces a
        traceable certificate is not a trade worth making.
        """
        rejected = []
        for name, value in (timing or {}).items():
            if not hasattr(self.driver_module, name):
                rejected.append(name)
                continue
            try:
                setattr(self.driver_module, name, float(value))
            except (TypeError, ValueError):
                rejected.append(name)
        return rejected


def check_conformance(instrument: Instrument) -> List[str]:
    """Return everything the core needs and this plugin does not provide.

    Checked at load, in one place, against name lists derived from what the core
    actually reaches for. The alternative -- finding out at the moment of use --
    means finding out mid-procedure with the instrument energised.
    """
    missing: List[str] = []

    def want(module, names, label):
        if module is None:
            return
        for name in names:
            if not hasattr(module, name):
                missing.append("%s is missing %s" % (label, name))

    want(instrument.driver_module, REQUIRED_DRIVER_MODULE, "the driver module")
    want(instrument.spec, REQUIRED_SPEC, "the spec module")
    want(instrument.perf_plan, REQUIRED_PERF, "the verification plan")
    if instrument.can("adjust"):
        want(instrument.adjust_plan, REQUIRED_ADJUST, "the adjustment plan")
    if instrument.can("calram"):
        want(instrument.driver_module, CALRAM_DRIVER_MODULE,
             "the driver module")

    try:
        cls = instrument.driver_class
    except PluginError as exc:
        missing.append(str(exc))
        return missing

    needed = list(REQUIRED_DRIVER_METHODS)
    if instrument.can("adjust"):
        needed += list(ADJUST_DRIVER_METHODS)
    if instrument.can("calram"):
        needed += list(CALRAM_DRIVER_METHODS)
    for name in needed:
        if not callable(getattr(cls, name, None)):
            missing.append("the driver class is missing %s()" % name)
    return missing


def discover(folder: Optional[str] = None) -> Tuple[List[Plugin], List[str]]:
    """Read every instrument manifest. Returns ``(plugins, errors)``.

    Imports nothing. A folder without a manifest is not an instrument and is
    passed over in silence; a manifest that will not parse is an error, because
    somebody meant it to be an instrument.
    """
    root = plugins_dir(folder)
    plugins: List[Plugin] = []
    errors: List[str] = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path) or name.startswith((".", "_")):
            continue
        manifest = os.path.join(path, MANIFEST)
        if not os.path.isfile(manifest):
            continue
        parser = configparser.ConfigParser()
        try:
            with open(manifest, "r", encoding="utf-8") as handle:
                parser.read_file(handle)
        except Exception as exc:                          # noqa: BLE001
            errors.append("%s/%s: %s" % (name, MANIFEST, exc))
            continue
        plugin = Plugin(path, parser)
        duplicate = next((p for p in plugins if p.key == plugin.key), None)
        if duplicate is not None:
            errors.append("%s and %s both claim the key %r"
                          % (duplicate.folder, plugin.folder, plugin.key))
            continue
        plugins.append(plugin)
    return plugins, errors


def by_key(key: str, folder: Optional[str] = None) -> Optional[Plugin]:
    """Find one plugin by its key."""
    plugins, _ = discover(folder)
    return next((p for p in plugins if p.key == str(key).lower()), None)


def match_scan(plugins: Sequence[Plugin],
               scan: Sequence[Tuple[str, Optional[str]]]
               ) -> List[Tuple[Plugin, str, str]]:
    """Pair plugins with the bus addresses that answered as them.

    Returns ``(plugin, resource, identity)`` per hit, in scan order, so an
    operator with two supported instruments on one bus is offered both rather
    than having the first one chosen for them.
    """
    hits = []
    for resource, identity in scan:
        for plugin in plugins:
            if plugin.matches(identity):
                hits.append((plugin, resource, identity))
                break
    return hits
