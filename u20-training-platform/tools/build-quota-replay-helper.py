#!/usr/bin/env python3
"""Build an isolated read-only WAL replay helper from the pinned controller source.

It extracts the actual 7a06cff replay and dependent functions. This does not
verify scheduler state, checkpoint/witness, storage durability or deployment.
"""
import argparse
import hashlib
from pathlib import Path
import subprocess

SOURCE_SHA='9c3c42de457cfec0bf929db5e9c06c79893e422252f0f1eacf54fba6c2c932fb'


def section(text,start,end):
    if text.count(start)!=1 or text.count(end)!=1: raise ValueError('source markers not unique')
    a=text.index(start); b=text.index(end,a)
    return text[a:b]


def build(source,output):
    if source.is_symlink() or not source.is_file(): raise ValueError('source must be a regular file')
    raw=source.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=SOURCE_SHA: raise ValueError('candidate source digest mismatch')
    text=raw.decode('utf-8')
    sections=[
        section(text,'typedef struct {\n\tuint64_t sec;\n\tuint32_t nsec;\n} mq_time_t;','static pthread_mutex_t mq_lock'),
        'static int mq_fd;\nstatic uint64_t mq_seq, mq_next_generation;\n'
        'static mq_month_record_t *mq_months;\nstatic mq_grant_record_t *mq_grants;\nstatic mq_active_record_t *mq_active;\n',
        section(text,'static int _time_cmp(','/* Howard Hinnant\'s'),
        section(text,'static int64_t _floor_div(','static int _now('),
        section(text,'static bool _field_safe(','static int _write_all('),
        section(text,'static mq_month_record_t *_find_month(','static void _mark_unhealthy('),
        section(text,'static int _parse_u64(','/* Recompute a digest over the complete locked WAL'),
    ]
    prefix='''#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>
#define MQ_WAL_VERSION "U20MQ1"
#define MQ_NSEC_PER_SEC 1000000000U
#define MQ_SECONDS_PER_DAY 86400LL
#define MQ_SHANGHAI_OFFSET (8LL * 60LL * 60LL)
#define MQ_MAX_FIELD 128
#define xcalloc(count, size) calloc(count, size)
#define xstrdup(s) strdup(s)
#define xfree(p) free(p)
'''
    main='''
int main(int argc, char **argv) {
    struct stat st;
    struct rlimit cpu = {5, 5};
    if (argc != 2) return 2;
    if (setrlimit(RLIMIT_CPU, &cpu)) return 2;
#ifdef __linux__
    struct rlimit memory = {536870912, 536870912};
    if (setrlimit(RLIMIT_AS, &memory)) return 2;
#endif
    mq_fd = open(argv[1], O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (mq_fd < 0 || fstat(mq_fd, &st) || !S_ISREG(st.st_mode) ||
        st.st_nlink != 1 || st.st_size < 0 || st.st_size > 134217728) return 2;
    mq_seq = 0; mq_next_generation = 1;
    if (_replay_wal()) { _free_memory(); close(mq_fd); return 1; }
    unsigned long active = 0;
    for (mq_active_record_t *item = mq_active; item; item = item->next) active++;
    printf("WAL_REPLAY_OK events=%" PRIu64 " active=%lu\\n", mq_seq, active);
    _free_memory(); close(mq_fd); return 0;
}
'''
    output.mkdir(mode=0o700,exist_ok=False)
    cfile=output/'quota-replay.c'; cfile.write_text(prefix+''.join(sections)+main)
    subprocess.run(['cc','-std=c99','-O2','-Wall','-Wextra',str(cfile),'-o',str(output/'quota-replay')],check=True)
    print('source_sha256='+SOURCE_SHA)
    print('helper_sha256='+hashlib.sha256((output/'quota-replay').read_bytes()).hexdigest())


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True,type=Path); parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args(); build(args.source,args.output)
