# Advantest R6142 — not yet implemented

This folder is a placeholder. It holds no `instrument.ini`, so the registry
does not list it and the tool is unaffected: a folder without a manifest is not
an instrument.

## What exists

Reference material is in the project's `Reference Manuals` folder:

* `Advantest R6142 Operation Manual.pdf`
* `Advantest R6142 R6144 Service Manual.pdf` — covers both
* `Advantest_R6142_R6144_U3_v02-04_M27C512_15F1_DIP28.bin` — the U3 firmware,
  64 KB, **shared between the R6142 and the R6144**

That the two share one firmware image is the useful fact here: one driver will
almost certainly serve both, with the range tables and the specification the
only real differences. Whether that is one plugin declaring
`models = R6142, R6144` or two plugins sharing a module is a decision for when
the manuals have been read, not before.

## What has to be established, in order

1. **The GPIB command set** — from the Operation Manual. Enough for
   verification on its own: range select, output value, operate/standby, sense
   and guard, output limits, and whatever `*IDN?` answers.
2. **The accuracy specification** — from the Operation Manual. This is
   `spec.py` and it is what every verdict rests on.
3. **The verification points and hookups** — `perf_plan.py`.

With those three a plugin is finished for verification, declares
`adjust = no`, and is genuinely useful: as-found and as-left certificates,
uncertainty budget, the lot.

4. **The calibration procedure** — from the Service Manual, if it documents
   one. The R6161's was in no manual at all and had to be recovered from
   firmware by disassembly, which is why that instrument's Technical Reference
   exists. If the R6142/R6144 Service Manual documents theirs, this is a
   reading exercise. If it does not, it is a disassembly project against the
   shared U3 image and should be treated as its own piece of work.

Do not guess a calibration command. An adjustment writes to EEPROM and there is
no undo.

## When it is ready

Copy `instruments/_template/README.md`'s five files into place. The contract
is checked at load and `support/test_instruments.py` says what is missing.
