# Staged bounded journal record reader

The candidate replay parser used `getline`, which can allocate memory proportional to a corrupt or adversarially oversized line. The staged reader accepts at most 1023 bytes including a terminating newline, matching the writer's 1024-byte buffer and `snprintf` limit. It rejects embedded NUL, overlong or truncated records and preserves the existing fail-closed recovery path. It reads through stdio into a fixed buffer and does not modify the journal.

Source review and compilation do not prove all boundary lengths, read errors, historical compatibility or crash recovery; those require authorized behavior validation. No live controller or journal was changed.
