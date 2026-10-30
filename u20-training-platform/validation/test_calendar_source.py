"""Check the candidate's actual calendar functions against Python dates.

Requires TIME_VALIDATION_SLURM_SOURCE to name a separate reconstructed source
tree. Builds a temporary shared object from the original function text; does
not connect to Slurm or touch its state.
"""
import ctypes
import datetime as dt
import os
import pathlib
import subprocess
import tempfile
import unittest


class CalendarSourceValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = pathlib.Path(os.environ["TIME_VALIDATION_SLURM_SOURCE"]).resolve(strict=True)
        text = (source / "src/slurmctld/monthly_quota.c").read_text()
        compare = text[text.index("static int _time_cmp("):text.index("static int _amount_add(")]
        calendar = text[text.index("static int64_t _floor_div("):text.index("static int _now(")]
        prefix = """#include <stdint.h>
#define MQ_SECONDS_PER_DAY 86400LL
#define MQ_SHANGHAI_OFFSET (8LL * 60LL * 60LL)
typedef struct { uint64_t sec; uint32_t nsec; } mq_time_t;
"""
        wrappers = """
uint32_t check_month(uint64_t sec) { mq_time_t t = {sec, 0}; uint32_t out = 0; return _month_key(&t, &out) ? 0 : out; }
uint64_t check_next(uint64_t sec) { mq_time_t t = {sec, 0}, out; return _next_month_boundary(&t, &out) ? 0 : out.sec; }
"""
        cls.temp = tempfile.TemporaryDirectory(prefix="time-calendar-")
        cfile = pathlib.Path(cls.temp.name) / "calendar.c"
        library = pathlib.Path(cls.temp.name) / "calendar.so"
        cfile.write_text(prefix + compare + calendar + wrappers)
        subprocess.run(["cc", "-std=c99", "-shared", "-fPIC", "-O2", str(cfile), "-o", str(library)], check=True)
        cls.lib = ctypes.CDLL(str(library))
        cls.lib.check_month.argtypes = [ctypes.c_uint64]
        cls.lib.check_month.restype = ctypes.c_uint32
        cls.lib.check_next.argtypes = [ctypes.c_uint64]
        cls.lib.check_next.restype = ctypes.c_uint64

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_month_and_next_boundary_across_years(self):
        zone = dt.timezone(dt.timedelta(hours=8))
        for year in range(1970, 2101):
            for month in range(1, 13):
                boundary = dt.datetime(year, month, 1, tzinfo=zone)
                timestamp = int(boundary.timestamp())
                for value in (timestamp - 1, timestamp, timestamp + 1):
                    if value < 0:
                        continue
                    local = dt.datetime.fromtimestamp(value, zone)
                    expected_month = local.year * 100 + local.month
                    next_year = local.year + (local.month == 12)
                    next_month = local.month % 12 + 1
                    expected_next = int(dt.datetime(next_year, next_month, 1, tzinfo=zone).timestamp())
                    self.assertEqual(self.lib.check_month(value), expected_month, (value, local))
                    self.assertEqual(self.lib.check_next(value), expected_next, (value, local))


if __name__ == "__main__":
    unittest.main()
