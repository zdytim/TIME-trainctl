# Staged bounded journal record reader

The candidate replay parser used `getline`, which can allocate memory proportional to a corrupt or adversarially oversized line. The staged reader accepts at most 1023 bytes including a terminating newline, matching the writer's 1024-byte buffer and `snprintf` limit. It rejects embedded NUL, overlong or truncated records and preserves the existing fail-closed recovery path. It reads through stdio into a fixed buffer and does not modify the journal.

Source review and compilation do not prove all boundary lengths, read errors, historical compatibility or crash recovery; those require authorized behavior validation. No live controller or journal was changed.

The committed patch `19e0da1` applied and compiled in the u20 isolated candidate. The controller ELF SHA-256 is `f09032b1c04b0895401e68c561faefac37bae416c2a3dafbbfe8630998cd4553`; existing Slurm and training services reported active after compilation. No candidate was installed or run.
