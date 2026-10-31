#!/usr/bin/env python3
"""Build the narrow capability-report fix for the deployed 0858424 CLI."""
import hashlib
from pathlib import Path
import sys

OLD='90a0c11296f30c8c379362198b272777d5d1a710fbef166280831cfeaca6d534'
NEEDLE=b'''                           reason="server reports the monthly gate is not enabled")\n    return {\n'''
REPLACEMENT=b'''                           reason="server reports the monthly gate is not enabled")\n    elif (isinstance(observed_monthly, dict) and\n          observed_monthly.get("enforcement_ready") is True and\n          observed_monthly.get("mode") == "enforcing"):\n        monthly.update(server_state="enforcing", enforcement_ready=True,\n                       accounting=observed_monthly.get("accounting", "unknown"),\n                       reason="server reports the verified monthly gate is active")\n    return {\n'''

def build(source):
    if hashlib.sha256(source).hexdigest()!=OLD or source.count(NEEDLE)!=1:
        raise ValueError('unexpected deployed CLI source')
    return source.replace(NEEDLE,REPLACEMENT,1)

if __name__=='__main__':
    if len(sys.argv)!=3:raise SystemExit('usage: build-u20-cli-capability-fix.py INPUT OUTPUT')
    source=Path(sys.argv[1]).read_bytes(); target=Path(sys.argv[2]); result=build(source)
    with target.open('xb') as out:out.write(result)
    print(hashlib.sha256(result).hexdigest())
