# Staged strict quota journal grant replay

The candidate's live GRANT path rejects invalid months and zero amounts and writes whole GPU-seconds. Its replay path previously accepted invalid months, zero or fractional amounts, and repeated GRANT events with the same ID. `strtoull` also accepted leading whitespace or a plus sign although the writer emits only decimal digits. These discrepancies can allow malformed records that the current writer could not produce to be accepted as history.

The staged patch requires decimal digits in numeric fields and applies the live GRANT bounds on replay, including positive whole seconds, valid year/month, and one journal event per globally unique grant ID. Idempotent retry remains supported by the live path without appending a second event. Previously generated nonconforming journals will require manual review and fail closed rather than being silently repaired.

Source review only; crash, historical compatibility, replay and grant behavior remain untested. The live controller is unchanged.

The committed patch `9622cab` applied and compiled in the u20 incremental candidate directory; its ELF controller SHA-256 was `d3fd4d18c137739bde87e2791e45e45fda7177ca1ae1f0eb7825a92bbd873cf2`. Existing Slurm and training services reported active after compilation. This did not execute or install the candidate or run a test.
