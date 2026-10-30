# Staged month-specific grant notification

An approved grant can remain in the durable outbox while its result is unknown and be reconciled after the month changes. The candidate previously notified the user that the controller confirmed an addition to "this month", even though the immutable grant targets the original month. The staged notification now states the target year and month from the outbox. It does not change, move, retry or apply any grant, and does not assert that previous-month debt carries into the new month.

This is a staged source change; notification, cross-month reconciliation and controller behavior have not been tested and the live broker is unchanged.
