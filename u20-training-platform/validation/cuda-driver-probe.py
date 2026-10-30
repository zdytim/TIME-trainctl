#!/usr/bin/env python3
"""Bounded real-GPU memory check inside the isolated Slurm fixture only."""
import ctypes as c
import os
from pathlib import Path

if os.environ.get('TIME_ISOLATED_VALIDATION') != '1' or not Path('/.dockerenv').is_file():
    raise SystemExit('isolated container required')
if os.getuid() != 1001:
    raise SystemExit('fixture training user required')
if os.environ.get('CUDA_VISIBLE_DEVICES') != '0':
    raise SystemExit('unexpected CUDA_VISIBLE_DEVICES')
cuda = c.CDLL('libcuda.so.1')

def check(name, *args):
    result = getattr(cuda, name)(*args)
    if result:
        raise RuntimeError('%s returned CUDA error %d' % (name, result))

check('cuInit', c.c_uint(0))
count = c.c_int()
check('cuDeviceGetCount', c.byref(count))
if count.value != 1:
    raise SystemExit('expected exactly one visible GPU')
device = c.c_int()
check('cuDeviceGet', c.byref(device), c.c_int(0))
name = c.create_string_buffer(128)
check('cuDeviceGetName', name, c.c_int(len(name)), device)
if b'4090' not in name.value:
    raise SystemExit('unexpected GPU model')
ctx = c.c_void_p()
check('cuCtxCreate_v2', c.byref(ctx), c.c_uint(0), device)
pointer = c.c_uint64()
try:
    check('cuMemAlloc_v2', c.byref(pointer), c.c_size_t(1024 * 1024))
    try:
        check('cuMemsetD8_v2', pointer, c.c_ubyte(0x5a), c.c_size_t(1024 * 1024))
        result = c.create_string_buffer(16)
        check('cuMemcpyDtoH_v2', result, pointer, c.c_size_t(16))
        if result.raw != b'\x5a' * 16:
            raise RuntimeError('GPU memory content mismatch')
    finally:
        check('cuMemFree_v2', pointer)
finally:
    check('cuCtxDestroy_v2', ctx)
print('CUDA_DRIVER_PROBE_OK ' + name.value.decode('ascii', 'replace') + ' 1048576 bytes')
