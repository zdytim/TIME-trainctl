#!/usr/bin/python3
"""Exercise an isolated PAM session stack, with no authentication or command execution."""
import ctypes as c
import ctypes.util
import sys

if len(sys.argv) != 4 or sys.argv[3] not in ('allow', 'deny'):
    raise SystemExit('usage: pam-session-check.py SERVICE USER allow|deny')
lib = c.CDLL(ctypes.util.find_library('pam'))
conversation = c.CFUNCTYPE(c.c_int, c.c_int, c.c_void_p, c.c_void_p, c.c_void_p)(
    lambda _n, _m, _r, _d: 19)

class Conversation(c.Structure):
    _fields_ = [('conv', c.c_void_p), ('appdata_ptr', c.c_void_p)]

conv = Conversation(c.cast(conversation, c.c_void_p), None)
handle = c.c_void_p()
lib.pam_start.argtypes = [c.c_char_p, c.c_char_p, c.POINTER(Conversation), c.POINTER(c.c_void_p)]
lib.pam_open_session.argtypes = [c.c_void_p, c.c_int]
lib.pam_close_session.argtypes = [c.c_void_p, c.c_int]
lib.pam_end.argtypes = [c.c_void_p, c.c_int]
status = lib.pam_start(sys.argv[1].encode(), sys.argv[2].encode(), c.byref(conv), c.byref(handle))
if status:
    raise SystemExit('pam_start failed: %d' % status)
opened = False
try:
    status = lib.pam_open_session(handle, 0)
    opened = status == 0
    if opened != (sys.argv[3] == 'allow'):
        raise RuntimeError('unexpected pam_open_session result: %d' % status)
    print('PAM session ' + ('accepted' if opened else 'refused') + ' as expected', flush=True)
finally:
    # pam_systemd may have created a scope before a later module denied open.
    close_status = lib.pam_close_session(handle, 0)
    if close_status:
        print('pam_close_session returned %d after %s open' %
              (close_status, 'successful' if opened else 'denied'), file=sys.stderr)
    lib.pam_end(handle, status)
if close_status and (opened or close_status != 6):
    raise SystemExit('session cleanup failed')
