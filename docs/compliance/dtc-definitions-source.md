# Fault-code definitions: source and licence

PRD 5 (Layer 2): "Check the licence of any source used to seed [`dtc_definitions`]."
Decision Q1 (Phase 4, 2026-11-23): **write our own definitions.**

## What we considered

| Source | What it is | Can we copy it into this repo? |
|---|---|---|
| SAE J2012 / ISO 15031-6 | The standard that defines generic DTCs and their official descriptions | **No.** The standard is a paid, copyrighted document; its exact wording can't be reproduced |
| python-OBD's code list (`obd/codes.py`) | A Python dict of generic codes and descriptions | **No.** It's GPL-2.0, which would require this project to be GPL too |
| Various "OBD code" websites and spreadsheets | Scraped or retyped lists | **No.** The licence is usually unstated or "all rights reserved", and the provenance is unclear |
| Manufacturer service information | Toyota/Lexus/Honda repair manuals | **No.** Copyrighted, and manufacturer codes are out of scope anyway (PRD 12) |

## What we did

`backend/app/config/dtc/definitions.yaml` is **written for AutoCare**, in our own plain
words:
- **The code number and the fact of what it covers** ("P0171 is the bank 1 lean
  condition") are facts about a standard and aren't copyrightable. The code list itself is
  public knowledge, printed in every scan tool.
- **The descriptions, causes, systems, severity tiers and stop-safely flags are our own**,
  written from general diagnostic knowledge. They're marked `SEED - validate` for Wisdom
  (automotive diagnostics background) to check and correct.
- No text was copied from any of the sources above.

Only **generic** codes are included (P0xxx, P2xxx, U0xxx and a few C0xxx/B0xxx). Codes a car
reports that aren't in the table still get a safe, plain answer ("not in our list yet").
They're never guessed by the LLM.
