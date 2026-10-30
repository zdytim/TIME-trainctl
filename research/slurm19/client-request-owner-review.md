# Staged request-status owner binding

The broker's idempotency keys are scoped by owner UID, but the CLI previously reconciled a local request against all visible jobs or applications using the request ID and payload. Administrators can see other owners' records. A coincident or deliberately reused request ID could therefore produce a false confirmed result for the administrator's own request.

The candidate resolves the current authenticated UID from `/api/v1/me` and requires each matching job or monthly application to have that exact integer `owner_uid`. If identity or ownership is unavailable, the record remains pending/unknown. The change does not grant access or change broker authorization; it avoids misattributing visible records. This applies to the candidate CLI only; the currently installed u20 client is unchanged. No identity or collision behavior tests were run.
