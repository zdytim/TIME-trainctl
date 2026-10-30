# Separate backend upgrades required for stronger guarantees

These are reviewable follow-up patches. They are intentionally absent from
this artifact and do not change the shared backend.

1. A staged additive `GET /api/v1/capabilities` response now reports schema,
   identity source, execution and log semantics, and a conservative monthly
   state. It is not installed on the current u20 broker. Full route and
   multi-node discovery and an authoritative controller state remain future
   work. Clients must not enable stronger behavior merely from this response.
2. Add a normalized request-body hash to the server's `(owner_uid,
   client_request_id)` record and return `idempotency_conflict` when a reused
   ID has different content.
3. Add additive envelope fields `schema_version`, `meta.request_id`,
   `meta.outcome`, `meta.retryable`, and stable error details. Preserve the
   legacy `ok/data/error` fields for Cockpit.
4. Add a native argv field with explicit server-side execution semantics, or
   retain the shell field while naming the shell boundary in the contract.
5. Add an authenticated request-status route keyed by the broker-owned
   request ID, plus allocation generation and explicit completion-unknown
   semantics. Do not accept a client UID as an identity selector.
6. Add offset/follow log semantics only after proving rotation, duplicate, and
   gap behavior. Keep the current bounded tail route unchanged.
7. Deliver monthly accounting and allocation enforcement as a separately
   activated controller. Until it can fail closed across restart, requeue,
   and unknown state, return `disabled` or `staged`, never `enforcing`.
8. If a remote shared MCP gateway is ever required, add a separately audited
   trusted actor mapping (mTLS or equivalent). A tool parameter, HTTP header,
   or model-supplied username is insufficient.
9. Add an independent host consent adapter for MCP mutations. It must issue a
   short-lived, one-shot, action/object-bound authorization outside tool
   arguments, bind the consent to the authenticated login user, and audit the
   grant. Until that exists, the MCP server must keep mutation tools absent and
   reject direct mutation calls.
