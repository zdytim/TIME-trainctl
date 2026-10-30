# Staged journal framing validation

The WAL replay parser used `strtok_r`, which collapses adjacent separators, and C string operations could ignore bytes after an embedded NUL. Consequently malformed lines with empty fields or hidden trailing bytes could be interpreted as valid records. The staged parser rejects embedded NULs and preserves empty fields so they are explicitly rejected. Lines emitted by the current writer use nonempty fields and are expected to be unaffected.

This protects only these framing cases; it is not an integrity checksum or a guarantee against coordinated history changes. Replay and historical compatibility require isolated behavior validation. No test or live controller change was performed.
