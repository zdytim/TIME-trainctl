# Staged cancellation status reconciliation

The CLI recorded a broker cancellation as `accepted_pending` when Slurm had not yet confirmed it, but `request-status` did not revisit that state. The candidate now looks up the exact job encoded in the original cancellation route. It reports confirmed only for an observed cancelled state, preserves pending while cancellation is reported in progress, and otherwise returns unknown along with the observed job rather than inferring that a completed or failed job was cancelled. The broker continues to control which jobs are visible, including an administrator's authorized targets.

If the job is missing from the broker's bounded listing, no new conclusion is made. This is a source change only; no cancellation, race, identity or transport behavior was exercised. The installed client is unchanged.
