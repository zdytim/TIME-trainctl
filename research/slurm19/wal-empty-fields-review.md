# Staged journal framing validation

The WAL replay parser used `strtok_r`, which collapses adjacent separators, and C string operations could ignore bytes after an embedded NUL. Consequently malformed lines with empty fields or hidden trailing bytes could be interpreted as valid records. The staged parser rejects embedded NULs and preserves empty fields so they are explicitly rejected. Lines emitted by the current writer use nonempty fields and are expected to be unaffected.

This protects only these framing cases; it is not an integrity checksum or a guarantee against coordinated history changes. Replay and historical compatibility require isolated behavior validation. No test or live controller change was performed.

On u20 the committed patch `3e2ac82` applied and the isolated controller compiled successfully; ELF SHA-256 `2137b051b190f9cfa0e12ed0b890cb7b6f7c2544178c631d42f942bd9077c1d8`. Existing Slurm and training services reported active after compilation. The candidate was not installed or run.
