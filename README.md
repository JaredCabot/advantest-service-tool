# Advantest Service, Verification and Adjustment Tool

A Python toolset for calibrating bench instruments over GPIB, using a reference
multimeter as the measurement standard. It automates the whole of a factory
calibration procedure, which these instruments otherwise only expose through
their front panels.

* **Performance verification** against the manufacturer's published accuracy
  specification, tagged *as-found* or *as-left*.
* **Full adjustment** — the complete factory routine, measured and entered
  automatically.
* **Calibration certificates** — ISO/IEC 17025-style PDFs in your choice of
  language, from a single saved run or from any two of them side by side as
  as-found and as-left, with your own uncertainty budget and decision rule.
* **Calibration-memory inspection** — read and decode the calibration block,
  and compare two images to see exactly what an adjustment changed.

## One tool, many instruments

Each instrument is a **plugin**: a folder under `instruments/` holding a
manifest and four Python modules. The tool reads the manifests at startup,
matches them against the GPIB bus, and loads only the one you are working on.
Adding an instrument means adding a folder — nothing in the core is edited, and
nothing in the core knows any instrument's name.

| Instrument | Verify | Adjust | Cal. memory |
|---|---|---|---|
| **Advantest R6161** DC Voltage/Current Generator | yes | yes | yes |
| Advantest R6142 | *not yet* | *not yet* | *not yet* |
| Advantest R6144 | *not yet* | *not yet* | *not yet* |

A plugin declares what it can actually do, and the menu offers exactly that.
There is no half-supported state where an item exists and fails when you choose
it — so an instrument whose calibration commands are undocumented can still
ship as a verify-only plugin and be genuinely useful.

See **Adding an instrument** below, and `instruments/_template/README.md`.

### About the R6161 plugin

The calibration commands it uses appear in no Advantest manual. They were
recovered by disassembling firmware A05, and are documented in
`R6161_Technical_Reference.pdf` alongside everything else about how that
instrument behaves on the bus.

> Much of what follows — the hookups, the current-range methods, the settling
> times, the specification figures — describes **the R6161**, because it is the
> instrument that exists. The machinery around it is not R6161-specific: the
> certificates, the uncertainty budget, the reference-meter handling and the run
> structure are the core's and apply to every instrument. Where a section is
> about one instrument's procedure it says so.

---

## What you need

* **Python 3.8 or later**, with **PyVISA** and **reportlab**
* A **GPIB interface and its VISA runtime** — National Instruments or Keysight
* A **reference multimeter**. In short, an 8½-digit one; see *Choosing a
  reference multimeter* below
* A **10 Ω standard resistor**, for the current-limit step of an adjustment
* Optionally **100 Ω and 1 kΩ standards** as well, which allow the better of the
  two ways of measuring the current ranges — see *Measuring the current ranges*
* Test leads, and **copper clips** for the divider output terminals

---

## Choosing a reference multimeter

The R6161's tightest specification is **32 ppm**, at 10 V and 100 V. A reference
standard is normally expected to be four times better than the instrument it
certifies, and that rules out more meters than people expect: a typical
6½-digit bench DMM is specified at 35–45 ppm at 10 V, which is *worse than the
R6161 itself*.

| Class | Typical 10 V spec | Ratio | Verdict |
|---|---|---|---|
| 5½–6½ digit | 35–45 ppm | ~1:1 or worse | Not usable |
| 7½ digit | 17–30 ppm | ~1.5:1 to 2:1 | Functional checks only |
| 8½ digit | 3–11 ppm | 3:1 to 10:1 | Suitable for certification |

Configs for meters that cannot do the job are deliberately not supplied, so the
selection menu cannot offer you a choice that would quietly produce an invalid
certificate. Eleven meters are included: Keysight 3458A; Fluke 8508A, 8588A and
8558A; Datron/Wavetek 1281; Prema 6048; Advantest R6581; Transmille 8081;
Keithley 2002; and two 7½-digit meters kept for specific reasons —

* **Keysight 34420A**, a nanovolt meter. Not good enough at 10 V, but the best
  available match for the divider ranges, where the R6161's own specification is
  loosest.
* **Keysight 34470A**, for functional checks and as-found readings. Below 4:1,
  so prefer an 8½-digit meter for a certificate.

The tool shows each meter's resolution class in the selection menu.

### Two meters in one run

If a 34420A is on the bus **alongside** another usable meter, you are offered the
choice of putting the divider ranges on it and leaving everything else on the
main meter:

```
A 34420A is connected via GPIB.
--------------------------------------------------------------------------
Use the 34420A for the Divider ranges? [y/N]:
```

Say yes and the right instrument is chosen for each group with nothing further
asked. The only extra work is at the bench: connect the divider output to the
34420A when that group comes up. The certificate then names **both** standards,
and asks you for the details of each.

The offer only appears when another meter is present — a 34420A alone cannot do
a run at all, having no DC current function and a 100 V ceiling.

---

## Getting started

```
python AdvantestServiceTool_v2.0.0.py
```

The tool **scans the GPIB bus and connects to what it finds** — there are no
addresses to type. It looks first for an instrument it has a plugin for, then
for a reference multimeter it has a config for. Where exactly one of each
answers, neither is a question; two or more is a real choice and is put to you.

```
Scanning the GPIB bus ...
   GPIB0::4::INSTR          ADVANTEST,R6161,REV A05
   GPIB0::22::INSTR         HP3458A
   Advantest R6161 DC Voltage/Current Generator recognised at GPIB0::4::INSTR
   reference: HP/Keysight 3458A at GPIB0::22::INSTR
```

If nothing on the bus matches a plugin, it lists what it does support and lets
you pick — an instrument may be silent for a reason worth checking rather than
absent.

If something is missing it says which, and why, and offers to rescan so you can
connect it without restarting:

```
 NO ADVANTEST R6161 FOUND
Every query is refused while the rear-panel EXT CAL switch
is in ENABLE, so a healthy instrument is silent in that
position. Check the switch before you check the cable.

  [R] rescan          connect it and try again
  [C] continue        go on anyway; you will be asked instead
  [Q] quit
```

Menu items 3 and 4 tell you how long they will take, worked out from the point
list and your own settling settings.

### A typical calibration

1. **Performance verification**, tagged **as-found** — the record of how the
   instrument arrived.
2. **Full adjustment**, if the as-found result warrants it.
3. **Performance verification**, tagged **as-left**.
4. **Generate a calibration certificate** — press Enter to take the pairing it
   offers.

Verifications are saved to `report_data/`, one file per run, and every run is
kept so you can build up a history.

Menu item 7 lists them newest first and asks for **one number, or two**:

```
Saved runs, newest first:

   #  Saved             Aspect    Points  Certificate
   -  ----------------  --------  ------  -------------
   1  2026-07-26 16:41  as-left       30  CAL-20260726-164106
   2  2026-07-26 14:02  as-found      30  CAL-20260726-140233
   3  2026-07-19 09:15  as-found      30  CAL-20260719-091522

   Every run listed: Performance Verification, serial 3021015

Enter ONE number for a single certificate, or TWO numbers
such as "1,3" for a combined as-found and as-left one.
Runs 1 and 2 are the as-left and the as-found of one
instrument, so they are offered as the default.
0 cancels.

Run(s) [1,2]:
```

One number certifies that run on its own; **any two** numbers put them side by
side as as-found and as-left, whichever rows they are — today's as-left against
a verification from a previous visit, or a rerun after a repair. The aspect tags
decide which of the two is the as-found, and where both carry the same tag the
earlier one is. Separators are not fussy: `1,2`, `1 2` and `1-2` all work.

A `Serial` column appears if the saved runs are not all for one instrument, and
combining two different serial numbers asks you to confirm first — a certificate
covers one instrument. **An adjustment produces no certificate**: it
records what the instrument was *told*, not what it does. What certifies an
adjusted instrument is the as-left verification taken afterwards. That is also
why an adjustment never asks you for serial numbers — the verifications do.

---

## What a run asks you, and when

Everything is asked **before the first hookup**, while you still have the
instruments and their certificates in front of you:

1. whether this run is the **as-found** or the **as-left** record
2. the **R6161's own serial number and options** — see below
3. whether a second meter should take the divider ranges, if one is on the bus
4. whether the current ranges are measured directly or as a voltage drop, and
   the calibrated value of each standard resistor if so
5. the serial number, calibration due date and traceability reference of every
   standard the run will use — meters and resistors alike

Where a meter has already reported its serial number over the bus it is offered
as the default, and you only have to press Enter. The 3458A predates that
convention and does not report one, so that is a number you type from the rear
panel.

### The instrument under test

Everything `*IDN?` carries is read from the instrument and shown rather than
asked, so the certificate describes what was actually on the bench:

```
 INSTRUMENT UNDER TEST
--------------------------------------------------------------------------
   Description    Advantest Voltage Current Source
   Model          R6161
   Firmware       A05

The R6161 reports no serial number and no option list, so those
two are typed. The serial number is on the rear-panel label.
Press Enter to accept the value in brackets.
   Serial number [-]:
   Options [-]:
```

Those two are the only item-under-test lines you enter. Both are remembered for
the rest of the session, so a verify–adjust–verify sequence asks once and takes
Enter thereafter, and both can be preset in `report_config.ini` under `[uut]` if
the bench sees the same instrument every time. Left blank they appear on the
certificate as `-`.

Each group then shows its heading and its connection, and asks:

```
 Divider Voltage Output
--------------------------------------------------------------------------
   R6161 DIVIDER OUT HI  ->  DMM INPUT HI
   R6161 DIVIDER OUT LO  ->  DMM INPUT LO
   ...
   The output is in STANDBY. Make the connection now.

Divider Voltage Output -- [R]un / [S]kip / e[X]it to menu [R]:
```

After that the run is uninterrupted, apart from the hookups themselves and — if
a group fails — the offer to run it again.

Every wait draws a progress bar. A run spends more of its time waiting than
doing anything else, and a silent minute is indistinguishable from a hung
instrument.

### If a group fails

A failing point is much more often the hookup than the instrument: a lead on the
wrong terminal, a meter still on its front inputs, a clip that has come off the
divider terminals. Rather than make you restart the whole verification, the tool
lists the failures at the end of the group and offers to measure it again. The
discarded attempt does not reach the certificate.

Use that to correct a mistake, not to fish for a better number. Repeating a
measurement until it passes is not a verification, and the certificate cannot
tell the difference between the two. You can.

---

## The hookups

The R6161 cannot present all ten ranges on one pair of terminals, so both
procedures are organised into groups that share a connection. You are prompted
once per group.

| Group | Connection | Ranges |
|---|---|---|
| Voltage Output | `+OUTPUT` / `−OUTPUT` to the DMM voltage input | 1 V, 10 V, 100 V, 1000 V |
| Divider Voltage Output | the separate **divider output** terminals | 10 mV, 100 mV, 1000 mV |
| Current Output — Direct Connect | the DMM **current** terminals, in series | 1 mA, 10 mA, 100 mA |
| Current Output — Standard Resistor | a standard across the output, DMM across it | one group per resistor |

The divider terminals are not optional: those three ranges read approximately
zero on the main output leads. Use copper clips, and allow the full minute the
tool waits after connecting — at 10 mV full scale a 1 µV thermal EMF is 100 ppm,
larger than the tolerance the reading is graded against.

---

## Measuring the current ranges

The manual offers two ways and does not say which is better. You are asked at
the start of both procedures, so that an as-left certificate and the adjustment
it certifies are traceable to the same standards:

```
 HOW SHOULD THE CURRENT RANGES BE MEASURED?
--------------------------------------------------------------------------
  1) Voltage drop across standard resistors, where you have them
     (Operation Manual 7.2.2(11)/(13)/(15): 1 kohm, 100 ohm, 10 ohm)
     Better on a 3458A -- its DC current function is the weaker
     one. You will be asked which of the three you have; any
     range you have no resistor for is measured directly.
  2) Direct current measurement through the meter's I terminals
     One hookup, no standards at all, and correct; simply a
     larger reference uncertainty on the 100 mA range.
  Method [2]:
```

**Option 2 needs nothing.** The meter's own current terminals go in series with
the generator: one hookup, no standards, perfectly correct.

**Option 1 asks about each of the three resistors in turn**, and takes them one
at a time — the ranges you have a standard for are measured as a voltage drop,
and the ranges you do not are measured directly on the same hookup as each other.
So one 10 Ω is worth having even with nothing else: it improves 100 mA, where it
matters most, and costs you nothing on 1 mA and 10 mA. Whatever you end up doing
is what the certificate states, range by range.

The drop method reads the voltage across a standard resistor and divides. The
manual specifies a *different* resistor per range, each chosen to put exactly
1 V across itself at that range's full scale:

| Range | Standard |
|---|---|
| 1 mA | **1 kΩ** |
| 10 mA | **100 Ω** |
| 100 mA | **10 Ω** |

Using one 10 Ω for all three — the obvious simplification — would read 10 mV at
1 mA, and be *worse* than measuring directly.

Which to choose depends on your meter. On a 3458A the DC current function is
much the weaker one, so the drop method wins on every range:

| Range | Direct | Voltage drop | R6161 90-day | Ratio, direct → drop |
|---|---|---|---|---|
| 1 mA | 26.0 | **13.5** | 69 | 2.7:1 → **5.1:1** |
| 10 mA | 26.0 | **19.8** | 54 | 2.1:1 → **2.7:1** |
| 100 mA | 41.0 | **22.6** | 54 | 1.3:1 → **2.4:1** |

*ppm of reading at full scale, one-year specification.* The 100 mA case is the
one that matters: measured directly, the reference is barely better than the
instrument it is grading.

For each resistor you can type the calibrated value, or have the tool measure it
four-wire on the reference meter first. Bear in mind that a standard is
characterised cold and used hot — 100 mA through a 10 Ω is 0.1 W — so a resistor
with a known power coefficient is worth having.

### The adjustment's current-limit step

One adjustment point has no direct equivalent in 7.2.2: the current-limit
coefficient, which the manual measures as the drop across the same 10 Ω. It is
never a question of its own — it follows whatever the **100 mA range** is doing,
because the two share a hookup either way:

- **With a 10 Ω**, it is the last point of that resistor's group, exactly as the
  manual has it.
- **Without one**, it is measured *directly*, as the last point of the
  direct-connect group. The meter's current terminals are close enough to a
  short that the generator sits in current limit, so the reading is the limit
  current itself — no resistance involved, and none that has to be known.

The direct route is not the manual's method. It removes a term rather than
adding one, and it is why option 2 needs no resistor anywhere, but the first run
that uses it is also the first evidence it behaves as expected: check that the
entered value lands near 50 mA before approving the table.

---

## Settings you can change

All configuration is human-readable and meant to be edited.

**A `USER SETTINGS` block at the top of the main tool** holds the interface
language and the four waits below. Anything you change is printed at startup and
shown under *Instrument information*, so a run is never made with a setting you
cannot see. A value that is not a number, or is below the floor for that
setting, is reported and ignored rather than silently substituted.

The settling times below are the R6161's. **They are properties of the
instrument, not of the tool**, so each plugin ships its own in its driver module
and may override them in `instrument.ini`:

```ini
[timing]
SETTLE_S = 8.0
```

The layering is: the plugin's driver holds the instrument's measured defaults,
`instrument.ini` may override them for your bench, and the `USER SETTINGS` block
overrides that for a session. A setting an instrument does not have — another
instrument may have no divider and so no thermal wait — is skipped rather than
reported as an error.

| Setting | R6161 default | What it is |
|---|---|---|
| `SETTLE_S` | **5 s** | Wait after OPERATE before a point is read, on all ranges except those below |
| `SETTLE_HV_S` | **20 s** | The same, on the 1000 V range |
| `SETTLE_100MA_S` | **60 s** | The same, on the 100 mA range |
| `THERMAL_WAIT_S` | **60 s** | Settling for thermoelectric EMF after a hookup change, on the divider group. Set to 0 to skip it |

Each figure is the manual's own, and the first two have been confirmed here by
measurement. Shortening them costs accuracy rather than producing an error,
which is what makes getting them wrong hard to notice — while a range is still
settling its error is always *negative*, so it is a bias that repeated readings
cannot average away.

**`report_config.ini`** holds your laboratory, customer and personnel details
for the certificate, selects which accuracy table to grade against, and can
preset the R6161's serial number and options under `[uut]` so the run's prompt
only needs Enter.

**`uncertainty.ini`** holds your measurement uncertainty budget and the decision
rule the verdict uses. Optional; blank means the certificate reports no budget.
See *Measurement uncertainty* above.

**`dmm_configs/`** holds one `.ini` per meter. Integration time and displayed
resolution are properties of the meter, so they live there rather than in the
tool — `nplc` (100 on the 3458A, where the manual asks for 20 or more) and
`ndig`. Set `nplc` to whatever you like; the I/O timeout follows it
automatically. `_EXAMPLE.ini` documents every key.

**`lang/`** holds the interface and certificate translations: English, German,
Spanish, French, Italian and Japanese.

---

## Before your first run

* **Read the header of the meter config you intend to use.** Most were written
  from the instrument's own programming manual; where a detail could not be
  sourced it is marked `# UNVERIFIED`, with a `# CONFIDENCE:` line at the top.
  The commonest unsourced item is the factory-default GPIB address, which
  several manufacturers never state — check your meter's front panel.
* **Two meters need a front-panel setting checked first.** The Advantest R6581
  has a command-language selector that cannot be reached over the bus; in the
  wrong position every command is silently rejected. The Fluke 8508A and Datron
  1281 free-run by default and must be switched to external triggering, which
  their configs do at connect time.
* **The EXT CAL switch is physical**, on the rear panel. No command can move it,
  so the tool prompts and verifies rather than assuming. An adjustment needs it
  in ENABLE; everything else needs it in DISABLE.
* **A meter's input limit is respected.** If a config declares one, both
  procedures skip any point that would exceed it rather than applying it — which
  is why the 34420A declines the 1000 V range instead of having 1000 V put
  across a 100 V input.

---

## Safety

* **High voltage.** The Voltage Output group energises the terminals to 1000 V,
  and the voltage-limit adjustment step generates approximately 500 V.
* **Nothing is committed until CAL END.** Adjustment entries accumulate in the
  instrument's RAM. Abandoning a run at any point, for any reason, leaves the
  stored calibration exactly as it was.
* **You approve the whole table** before CAL END is sent.
* **Every reading is validated before it is entered.** A reading must fall
  inside a plausibility window and two consecutive readings must agree, so a
  silent hardware failure cannot enter a wrong value that merely looks right.
* **The commit is verified against the instrument's memory.** After CAL END the
  calibration block is re-read and compared with the image taken beforehand.
  *No record changed* is reported as a failure, not a remark — an accepted entry
  is not necessarily a stored one.
* **`Z` and `*RST` are refused by the driver.** Both discard the calibration.
* **The instrument is left as it leaves the factory** — output in standby,
  EXT-SENSE and EXT-GUARD off. Both are battery-backed, so whatever a run leaves
  behind survives the next power cycle.

There is deliberately **no restore counterpart** to the calibration-image read.
Only the firmware's own commit can write the calibration, so an image can be
read for comparison but not written back. Keep the images anyway: if a
calibration is ever lost, they tell you what to re-enter.

---

## Measurement uncertainty

The tool cannot evaluate an uncertainty budget for you. It knows what it
measured; it does not know your 3458A's accuracy at that reading, that meter's
own calibration uncertainty, your standard resistors' tolerances, or the thermal
EMF of your interconnects. What it can do is hold **your** evaluated budget and
apply it, which is `uncertainty.ini`.

**Leave it blank and nothing changes.** The uncertainty column reads `-` and the
certificate states plainly that no budget is reported. That is the honest default
for a bench that has not done the work.

Fill it in and every point gets an expanded uncertainty evaluated at its own
reading and range — and the certificate then also rounds each measured value and
deviation to the resolution that uncertainty justifies, per GUM JCGM 100:2008
§7.2.6 and ILAC P14 §6.3. Grading still uses full precision.

### Writing a figure, either way round

Every function section reads the same three keys, and one flag that says what
they are:

```ini
[dcv]
expanded    = no     # they are STANDARD uncertainties (k=1), to be expanded by k
ppm_reading = 3.5    # ppm of the reading
ppm_range   = 0.5    # ppm of the range's full scale
floor_uv    = 0.3    # an absolute floor, in microvolts (current: floor_na, in nA)
```

Set `expanded = yes` and the same three numbers are taken as **already expanded**
at the stated `k` — which is the form a calibration certificate for your
reference meter usually quotes ("±3 ppm of reading, k = 2"). So you can copy
figures across in whichever form your paperwork uses; only the flag differs.

Sections: `[dcv]` for the main output ranges, `[divider]` for 10/100/1000 mV
(separate because it is often a different meter, and because at 10 mV full scale
one microvolt is 100 ppm — `floor_uv` is a leading term there, not a rounding
error), `[dci]` for current measured directly, `[shunt]` for the meter's *voltage*
figures when current is measured as a drop, and `[resistor]` for the standards'
own ppm per range. A per-range section such as `[dcv.1000V]` overrides its
function's.

### How the terms go together

`combine` decides, because it depends on what your figures are:

```
combine = rss      u = sqrt( (ppm_reading × reading)² + (ppm_range × fs)² + floor² )
combine = linear   u = ppm_reading × reading + ppm_range × fs + floor
```

**A data-sheet figure almost always wants `linear`**, for the same reason it
wants `expanded = yes`: "20 ppm of reading + 5 ppm of range" is *one* limit whose
parts add, not two independent uncertainties. Combining a specification's own
terms in quadrature understates it — on a 3458A's 100 mA range you get 36 ppm
where the specification says 41. Use `rss` for a budget you assembled yourself
from genuinely independent contributions. Set it in `[general]`, override it per
section.

The observed spread joins in quadrature either way, since it is independent of
the reference's specification however that specification's own terms went
together:

```
U = k × sqrt( u² + spread² )
```

`spread` is the agreement between the two readings the tool already takes at
every point — a type-A term it measures for itself, included unless you set
`include_observed_spread = no`. A voltage-drop point additionally picks up its
resistor's ppm (since I = V/R makes a relative error in R a relative error in the
current) and has the meter's microvolt floor divided by the resistance.

The certificate states which way the terms were combined, along with `k`, the
decision rule, and whether the spread was included.

### The decision rule

`decision_rule` decides whether any of this touches PASS/FAIL:

| | |
|---|---|
| `simple` | ILAC-G8 §4.2.1 simple acceptance. Verdict stays \|deviation\| ≤ specification, with U reported beside it. Nothing that passes on specification alone starts failing. |
| `guarded` | \|deviation\| ≤ specification − U. A point too close to the limit to resolve is a fail. |

Guard-banding bites hardest where the reference is weakest. What it costs, using
the 3458A figures from Technical Reference table 6-6:

| Range | R6161 90-day | Direct: band left | Voltage drop: band left |
|---|---|---|---|
| 1 mA | 69 ppm | 43 ppm (62 %) | **56 ppm (80 %)** |
| 10 mA | 54 ppm | 28 ppm (52 %) | **34 ppm (63 %)** |
| 100 mA | 54 ppm | 13 ppm (24 %) | **31 ppm (58 %)** |

On 100 mA measured directly, three quarters of the instrument's own
specification is consumed by the uncertainty of the reference. That is the
strongest argument there is for the voltage-drop method — and it only appears
once you turn guard-banding on. Where U actually *exceeds* the specification the
row is left **ungraded** rather than failed, because that is the reference's
shortcoming and not the instrument's, and the certificate reports it as not
specified.

> **Two traps worth naming**, both of which come from treating a specification
> limit as though it were a standard uncertainty.
>
> `expanded = no` on a data-sheet figure multiplies it by `k` a second time — it
> already contains its own coverage — doubling every uncertainty on the
> certificate. `combine = rss` on a two-term specification quietly *shrinks* it,
> because the two halves of "a ppm of reading + b ppm of range" are meant to be
> added. The first is pessimistic, the second flattering; neither is what the
> specification says.
>
> Data-sheet figures want `expanded = yes` and `combine = linear`. Reserve
> `expanded = no` and `combine = rss` for a budget you assembled yourself out of
> independent contributions.

Whichever you choose, **the certificate states the rule it used**, along with `k`
and whether the observed spread was included. The statement is generated from the
budget that was actually applied, so it cannot claim simple acceptance over
guard-banded rows.

The run tells you which it loaded, before the first hookup:

```
Uncertainty budget loaded: k = 2, simple acceptance.
```

or, if there is nothing to load:

```
No uncertainty budget in uncertainty.ini; the certificate will
state that none is reported.
```

---

## What's in the folder

Only the first file is run directly.

```
AdvantestServiceTool_v2.0.0.py         the program you run

instruments\                           one folder per supported instrument
  r6161\                                 the Advantest R6161
    instrument.ini                         identity, capabilities, defaults
    driver.py                              the instrument over GPIB
    spec.py                                its published accuracy specification
    perf_plan.py                           what a verification measures
    adjust_plan.py                         what an adjustment sets
  _template\README.md                    how to add another

instruments.py                         finds and loads instrument plugins
active.py                              which instrument this session is using
units.py                               reading display, scaling, progress bars
perf_engine.py  adjust_engine.py       runs a verification / an adjustment
procedure.py  standards.py             shared between the two procedures
dmm_driver.py                          the reference multimeter
cal_report.py  cert_report.py          the certificate, and the PDF it is drawn in
uncertainty.py                         your uncertainty budget and decision rule
i18n.py                                translations

report_config.ini                      laboratory and customer details
uncertainty.ini                        your uncertainty budget (optional)
dmm_configs\                           one .ini per supported multimeter
lang\                                  translation files, one folder per language
R6161_Technical_Reference.pdf          how the R6161 works, in detail
R6161_Example_Certificate.pdf          what a finished certificate looks like

cal_images\                            calibration-memory images
report_data\                           saved verification results
reports\                               generated certificates
```

The last three are created on first run, and nothing is written outside them.

---

## Adding an instrument

Copy `instruments/_template/README.md`'s five files into a new folder under
`instruments/`. Nothing in the core is edited.

```
instruments/r6142/
    instrument.ini      identity, capabilities, defaults   (read at startup)
    driver.py           the instrument over GPIB           (imported on use)
    spec.py             the published accuracy specification
    perf_plan.py        what a verification measures
    adjust_plan.py      what an adjustment sets            (only if adjust = yes)
```

**Start with the capabilities, not the code.** `instrument.ini` declares what
the plugin can do:

```ini
[capabilities]
verify = yes
adjust = no
calram = no
```

An instrument whose calibration commands are undocumented is still worth
having: with `adjust = no` there is no `adjust_plan.py` to write, no adjustment
menu item, and no calibration-memory items — and you still get verification,
as-found and as-left certificates, and the uncertainty budget. **A useful plugin
can be finished from an Operation Manual alone.** The adjustment usually needs
more than that.

**The contract is checked when the plugin loads**, against lists in
`instruments.py` of what the core actually reaches for, and it names what is
missing:

```
that plugin cannot be used: r6142: the driver module is missing
HOOKUP_OVERHEAD_S; the driver class is missing operate()
```

Better than finding out forty minutes into an adjustment, which is the point.
`support/test_instruments.py` is the executable version of the same
specification — most of its cases build plugins the core has never seen, because
a registry that only works for the plugin it was written alongside is not a
registry.

**Discovery imports nothing.** Manifests are read at startup so the tool can
list, match and report; a plugin's Python is imported only once you select that
instrument. A plugin that will not import costs its own menu entry and nothing
else.

**Do not reimplement the reading display.** `units.py` in the core holds the
unit scaling, the nine-significant-figure reading format and the progress bar.
Re-export them from your driver as the R6161's does, or two instruments'
certificates will round differently.

**Never guess a calibration command.** An adjustment writes to EEPROM and there
is no undo.

---

## Further reading

**`R6161_Technical_Reference.pdf`** — the firmware, memory map, calibration data
format, the complete GPIB command reference including the undocumented
calibration commands, the error codes, the instrument's transport behaviour, and
the bench measurements behind every figure quoted above. Seventy-odd pages,
generated from the same constants the tool runs on, so the two cannot drift
apart.

**`R6161_Example_Certificate.pdf`** — a finished certificate, so you can see what
you are going to get before you run anything. A synthetic instrument whose
divider network has aged: ten as-found failures on the 10 mV and 100 mV ranges, a
clean as-left pass after adjustment, a full uncertainty budget, and conformity
assessed with a guard band. Worth looking at the `100mA +F.S` as-found row — a
deviation well inside the tolerance that still reads FAIL, because the reference
cannot resolve the specification there. That is the argument for the voltage-drop
method, on paper.

## Not included here

Development lives alongside this tool but is not part of it, and is kept out of
the repository so that what you download is the thing you run: the builder that
generates the technical reference, an offline test suite of nine harnesses that
model the instrument's behaviour so a broken run fails a test rather than a
bench session, and the bench utilities behind the measurements. Ask if you want
any of it.

If you are writing an instrument plugin, `support/test_instruments.py` is the
one worth having: it is the executable form of the contract in
`instruments.py`, and most of its cases build plugins from scratch in temporary
folders rather than testing against the R6161.

## Credits

* **Claude Opus 4.5 and Claude Opus 5 (Anthropic)** — firmware
  reverse-engineering and software development.
* **Jared Cabot** — reverse-engineering, bench verification and tool development.

## Licence

Released under the MIT Licence.
