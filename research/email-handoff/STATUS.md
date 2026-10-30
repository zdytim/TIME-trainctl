# Email delivery handoff — incomplete

This is an isolated, unintegrated draft recovered from u20 after the worker failed; it is not deployed and does not implement the requested direct email approval links. No credentials or runtime database are included. SMTP host certificate verification, a reachable FRP web entry, administrator identity and secure single-use approval flows remain unresolved.

Mac-side review prevents using SMTP credentials without TLS and opens the root-owned credentials file with O_NOFOLLOW and descriptor-based verification. The change has received a Python syntax check only; no tests or messages were run. The implementation still needs end-to-end review and integration; in particular, durable outbox failure handling, approval token authorization and event reconciliation require further work.
