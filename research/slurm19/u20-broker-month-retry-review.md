# Staged monthly request retries across month boundaries

The previous candidate validated that an application targets the current Beijing month before looking up its `(owner UID, client request ID)` record. A successful application whose response was lost could therefore no longer be retried after month rollover, despite the advertised idempotency contract. Controller disconnection or feature disablement also prevented retrieving the existing result through this path.

The incremental patch validates the request's shape, looks up the authenticated owner's key and returns the existing application only when its complete normalized payload and reason match. A different payload remains a conflict. If no record exists, it checks that the month is current and verifies that the feature is enabled before creating a new application. Approval continues to require the current month, and this change does not move grants across months. A lost historical result can also be looked up through the application's ordinary visibility-filtered listing.

This is a source review and staged change only. Concurrency, month-boundary behavior, authorization and crash recovery still require explicit behavior validation before deployment. No test, service restart, application, grant or live database mutation was performed.

The committed candidate at `216a0588e07d3d34c77b335d743d6dded343551b` was independently assembled and AST-parsed on the Mac and in `/home/u20/.local/share/time/builds/repro-broker-216a058` on u20. Both yielded broker SHA-256 `aa9399ec29c7b2a615cf1bd08a2c75e5d59df50b359d91bfab62705cbfecd2e2`. The candidate was not executed or installed.
