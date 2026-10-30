# Staged MCP request status

The staged consent-enabled MCP can submit mutations and return a request ID when the outcome is unknown, but previously lacked a corresponding request-status tool. The candidate adds `time_request_status`, using the same user-local ledger and reconciliation logic as `timectl request-status`. It does not resend the original action. Submit and application matching require the authenticated owner; cancellation is matched to its explicit job ID and broker-visible jobs. An inconclusive lookup remains unknown. Access can update the local request ledger; it does not issue a broker mutation.

The tool is available in the staged read-only tool set as well as the consent-enabled set, but requires accessible local request state. It is not present in the installed u20 MCP, and no protocol, permissions, crash or behavior test was run. The candidate remains uninstalled.
