#!/usr/bin/env python3
"""Verify enrolled user cannot submit a delayed, harmless GPU job directly."""
import os
import pwd
import re
import subprocess
import sys
import uuid

if pwd.getpwuid(os.geteuid()).pw_name != "timzhang":
    raise SystemExit("run only as the designated timzhang test account")
name = "time-direct-probe-" + uuid.uuid4().hex[:12]
result = subprocess.run([
    "sbatch", "--parsable", "--begin=now+10minutes", "--gres=gpu:1",
    "--mem=64M", "--time=00:01:00", "--job-name=" + name,
    "--wrap=echo harmless-direct-probe",
], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
if result.returncode:
    if "Please submit through the Cockpit training platform" not in result.stderr:
        print("UNEXPECTED rejection:", result.stderr.strip())
        sys.exit(2)
    print("PASS: direct sbatch rejected for enrolled user")
    sys.exit(0)
job_id = result.stdout.strip().split(";")[0]
if not re.fullmatch(r"[0-9]+", job_id):
    print("FAIL: accepted submission returned an unknown job ID; inspect queue:", result.stdout.strip())
    sys.exit(1)
cancel = subprocess.run(["scancel", job_id], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
print("FAIL: direct submission accepted; test job", job_id,
      "cancellation exit", cancel.returncode, cancel.stderr.strip())
sys.exit(1)
