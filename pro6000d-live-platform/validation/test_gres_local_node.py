"""Exercise the real controller's GRES guard with Slurm's node iterator semantics."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class LocalNodeGuardTest(unittest.TestCase):
    def test_one_local_node_and_rejects_other_topologies(self):
        source = os.environ.get('SLURM26_SOURCE')
        if not source:
            self.skipTest('set SLURM26_SOURCE to the assembled Slurm 26 source')
        text = (Path(source)/'src/slurmctld/monthly_quota.c').read_text()
        start = text.index('static bool _supported_gres_device_order(void)')
        opening = text.index('{', start)
        depth = 1
        end = opening + 1
        while depth:
            if text[end] == '{': depth += 1
            if text[end] == '}': depth -= 1
            end += 1
        function = text[start:end]
        harness = r'''
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#define MQ_GPU_MAP_MAX 128
#define xfree free
typedef struct { char *name; char *node_hostname; } node_record_t;
static bool mq_test_inventory=false;
static unsigned mq_gpu_inventory_count=4;
static int node_count;
static char *config;
static node_record_t nodes[]={{"pro6000d","ubuntu"},{"other","ubuntu"}};
static node_record_t *next_node(int *index) { return *index<node_count ? &nodes[*index] : NULL; }
static int fake_gethostname(char *out, size_t n) { snprintf(out,n,"ubuntu"); return 0; }
#define gethostname fake_gethostname
static char *get_extra_conf_path(const char *ignored) { (void)ignored; return strdup(config); }
''' + function + r'''
int main(int argc, char **argv) {
    if (argc != 4) return 99;
    node_count=atoi(argv[1]); config=argv[2]; nodes[0].node_hostname=argv[3];
    return _supported_gres_device_order() ? 0 : 1;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp); (tmp/'test.c').write_text(harness)
            config=tmp/'gres.conf'; config.write_text('AutoDetect=off\nName=gpu File=/dev/nvidia[0-3]\n')
            subprocess.run(['cc','-std=c11','-D_POSIX_C_SOURCE=200809L',str(tmp/'test.c'),'-o',str(tmp/'test')],check=True)
            def check(count,host='ubuntu'):
                return subprocess.run([str(tmp/'test'),str(count),str(config),host]).returncode
            self.assertEqual(check(1),0)
            self.assertEqual(check(0),1)
            self.assertEqual(check(2),1)
            self.assertEqual(check(1,'other'),1)
            config.write_text('AutoDetect=off\nName=gpu File=/dev/nvidia[0-2]\n')
            self.assertEqual(check(1),1)


if __name__ == '__main__': unittest.main()
