# Security and trust boundaries

- The only identity is the Unix socket peer credential established by the
  root broker. The client never accepts a UID, username, or role as authority.
- The client never opens SQLite, invokes Slurm commands, uses `sudo`, or
  creates a privileged gateway. Direct user `sbatch` remains subject to the
  existing admission policy.
- Request IDs and request bodies are stored in a current-user-owned directory
  with mode 0700, a mode 0600 lock, and a mode 0600 atomic ledger file. Path
  components and files must not be symlinks. Reservation is a locked
  read/compare/write transaction, and a reused ID with a different body or a
  tampered stored hash is rejected locally.
- A write timeout is ambiguous. The client records `unknown` and does not
  issue a second mutation. Recovery is a read-only jobs query by durable
  `client_request_id`.
- MCP mutation tools are withheld and direct mutation calls fail closed because
  AI-supplied `confirm`/`user_intent` fields are not independent human consent.
  A future integration must pass a host-issued, action-bound, one-shot consent
  token out of band. Tool arguments contain no impersonation field. Logs are
  data returned to the caller and are never interpreted as instructions.
- The legacy backend accepts a shell command. The client quotes each supplied
  argv item with POSIX shell quoting and clearly reports that this is not a
  native argv API. Users must explicitly request shell features.
- The installer refuses root and prefixes outside the current user's home,
  refuses overwrites, and rolls back only hash-matching files from its own
  manifest. It does not modify system services, Slurm, or backend versions.
- Monthly quota is shown as unknown when runtime capability discovery is absent;
  the client makes no enforcement claim and refuses monthly requests.

- A request ID already present in the per-user ledger cannot be sent again,
  including when its outcome is pending or unknown. Inspect `timectl
  request-status <id>` first. This local guard cannot prevent retries made
  from a different machine or state directory; server-side payload binding is
  still required for cross-device idempotency.
