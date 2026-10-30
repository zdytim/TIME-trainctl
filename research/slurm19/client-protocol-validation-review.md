# Prepared client protocol validation

`u20-training-platform/validation/test_client_protocols.py` supplies isolated fake-socket checks for controller snapshot parsing, incomplete replies, uncertain grant sends, request field validation and pre-send unavailability. It also checks action-digest binding and unsupported action rejection in the terminal consent host. It loads explicitly selected assembled candidates and uses no actual socket, terminal, database or GPU. The fake does not verify Linux peer credentials, real I/O, Slurm behavior or human approval.

Only Python AST parsing was performed; no test was executed. Run only after the user's explicit test authorization and within the planned isolated environment.
