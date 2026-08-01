# Adding an instrument

Copy this folder, rename it, and fill in the five files. Nothing in the core is
edited and nothing in the core learns the instrument's name: the manifest is
read at startup, the modules are imported only when the instrument is chosen,
and `support/test_instruments.py` checks the contract against plugins it has
never seen.

    instrument.ini      identity, capabilities, defaults      (read at startup)
    driver.py           the instrument over GPIB              (imported on use)
    spec.py             the published accuracy specification
    perf_plan.py        what a verification measures
    adjust_plan.py      what an adjustment sets                (only if adjust = yes)

## Start with the capabilities, not the code

An instrument whose calibration commands are undocumented is still worth having:
set `adjust = no`, write no `adjust_plan.py`, and the tool offers verification
and certificates and no adjustment item at all. There is no half-supported state
where a menu item exists and fails when it is chosen. `calram = no` likewise
drops the two calibration-memory items.

So a useful plugin can be finished from an Operation Manual alone. The
adjustment usually needs more than that.

## What the core will reach for

`support/test_instruments.py` is the specification, and it names what is
missing rather than letting a run find out. The lists live in `instruments.py`
as `REQUIRED_*`; the short version:

* **driver module** — `RANGES`, `SETTLE_S`, `THERMAL_WAIT_S`,
  `INITIAL_EXT_SENSE`, `INITIAL_EXT_GUARD`, `POINT_OVERHEAD_S`,
  `HOOKUP_OVERHEAD_S`, and for `calram` the block helpers.
  Optionally `CURRENT_RANGES` and `MANUAL_SHUNTS` if the instrument has current
  ranges the manual measures as a voltage drop.
* **driver class** — twelve methods for verification, five more for adjustment,
  one for calibration memory. Name it in `[modules] driver_class`.
* **spec** — `tolerance()`, `INTERVAL_NAMES`, `DEFAULT_INTERVAL`,
  `conditions_note()`, `specification_note()`.
* **perf_plan** — `plan()`, `display_value()`, `measured_si()`, and the two
  safe output limits.
* **adjust_plan** — `groups()`, `units()`, `entry_value()`, `check_bounds()`,
  and the rest, only when `adjust = yes`.

## Reading display is not yours to write

`units.py` in the core holds the unit scaling, the nine-significant-figure
reading format and the progress bar. Re-export them from your driver as the
R6161's does; do not reimplement them, or two instruments' certificates will
round differently.

## The plugin's modules import each other by plain name

The folder goes on the path when the plugin loads, so `perf_plan.py` writes
`from driver import RANGES`. Two instruments may both have a module called
`driver` without colliding: the loader namespaces them by key.
