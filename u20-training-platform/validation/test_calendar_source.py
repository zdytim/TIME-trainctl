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


@unittest.skipUnless(os.environ.get("TIME_VALIDATION_SLURM_SOURCE"), "requires isolated Slurm source")
class CalendarSourceValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = pathlib.Path(os.environ["TIME_VALIDATION_SLURM_SOURCE"]).resolve(strict=True)
        text = (source / "src/slurmctld/monthly_quota.c").read_text()
        compare = text[text.index("static int _time_cmp("):text.index("/* Howard Hinnant's")]
        calendar = text[text.index("static int64_t _floor_div("):text.index("static int _now(")]
        find_month = text[text.index("static mq_month_record_t *_find_month("):text.index("static mq_active_record_t *_find_active(")]
        charge = text[text.index("static int _charge_interval("):text.index("static void _remove_active(")]
        declarations = text[text.index("typedef struct {\n\tuint64_t sec;"):text.index("static pthread_mutex_t mq_lock")]
        prefix = """#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#define MQ_NSEC_PER_SEC 1000000000U
#define MQ_SECONDS_PER_DAY 86400LL
#define MQ_SHANGHAI_OFFSET (8LL * 60LL * 60LL)
#define xcalloc(count, size) calloc(count, size)
"""
        wrappers = """
uint32_t check_month(uint64_t sec) { mq_time_t t = {sec, 0}; uint32_t out = 0; return _month_key(&t, &out) ? 0 : out; }
uint64_t check_next(uint64_t sec) { mq_time_t t = {sec, 0}, out; return _next_month_boundary(&t, &out) ? 0 : out.sec; }
void clear_months(void) { while (mq_months) { mq_month_record_t *next = mq_months->next; free(mq_months); mq_months = next; } }
int charge_interval(uint64_t start, uint32_t start_ns, uint64_t end, uint32_t end_ns, uint64_t gpu) {
    mq_time_t from = {start, start_ns}, to = {end, end_ns};
    mq_active_record_t active = {0}; active.uid = 1001; active.gpu_count = gpu;
    return _charge_interval(&active, &from, &to);
}
int read_charge(uint32_t month, uint64_t *seconds, uint32_t *nanos) {
    mq_month_record_t *record = _find_month(1001, month, false);
    if (!record) return -1;
    *seconds = record->usage.sec; *nanos = record->usage.nsec; return 0;
}
"""
        cls.temp = tempfile.TemporaryDirectory(prefix="time-calendar-")
        cfile = pathlib.Path(cls.temp.name) / "calendar.c"
        library = pathlib.Path(cls.temp.name) / "calendar.so"
        cfile.write_text(prefix + declarations + "static mq_month_record_t *mq_months;\n" + compare + calendar + find_month + charge + wrappers)
        subprocess.run(["cc", "-std=c99", "-shared", "-fPIC", "-O2", str(cfile), "-o", str(library)], check=True)
        cls.lib = ctypes.CDLL(str(library))
        cls.lib.check_month.argtypes = [ctypes.c_uint64]
        cls.lib.check_month.restype = ctypes.c_uint32
        cls.lib.check_next.argtypes = [ctypes.c_uint64]
        cls.lib.check_next.restype = ctypes.c_uint64
        cls.lib.charge_interval.argtypes = [ctypes.c_uint64, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_uint32, ctypes.c_uint64]
        cls.lib.charge_interval.restype = ctypes.c_int
        cls.lib.read_charge.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint32)]
        cls.lib.read_charge.restype = ctypes.c_int

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

    def test_four_gpu_charge_splits_at_shanghai_month_boundary(self):
        boundary = int(dt.datetime(2026, 11, 1, tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp())
        self.lib.clear_months()
        self.assertEqual(self.lib.charge_interval(boundary - 1, 500_000_000, boundary + 2, 250_000_000, 4), 0)
        for month, expected in ((202610, (2, 0)), (202611, (9, 0))):
            seconds, nanos = ctypes.c_uint64(), ctypes.c_uint32()
            self.assertEqual(self.lib.read_charge(month, ctypes.byref(seconds), ctypes.byref(nanos)), 0)
            self.assertEqual((seconds.value, nanos.value), expected)
        self.lib.clear_months()

    def test_four_gpu_charge_spans_multiple_months(self):
        zone = dt.timezone(dt.timedelta(hours=8))
        november = int(dt.datetime(2026, 11, 1, tzinfo=zone).timestamp())
        december = int(dt.datetime(2026, 12, 1, tzinfo=zone).timestamp())
        self.lib.clear_months()
        self.assertEqual(self.lib.charge_interval(november - 60, 0, december + 30, 0, 4), 0)
        for month, expected in ((202610, 240), (202611, (december - november) * 4), (202612, 120)):
            seconds, nanos = ctypes.c_uint64(), ctypes.c_uint32()
            self.assertEqual(self.lib.read_charge(month, ctypes.byref(seconds), ctypes.byref(nanos)), 0)
            self.assertEqual((seconds.value, nanos.value), (expected, 0))
        self.assertNotEqual(self.lib.charge_interval(december, 0, november, 0, 4), 0)
        self.lib.clear_months()


if __name__ == "__main__":
    unittest.main()
