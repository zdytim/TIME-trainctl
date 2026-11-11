"""Exercise the controller's real proof reader against a concurrent writer."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HARNESS=r'''
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#define MQ_NSEC_PER_SEC 1000000000U
typedef struct { uint64_t sec; uint32_t nsec; } mq_time_t;
typedef struct { uint32_t job_id, uid; uint64_t sluid; bool release_started, proof_seen;
 uint32_t final_state, final_exit, final_derived; uint64_t final_start, final_end;
 char final_node[128]; mq_time_t proof_at; } mq_active_record_t;
static int mq_nodeproof_fd; static off_t mq_nodeproof_offset; static mq_active_record_t rec;
static int _now(mq_time_t *t){struct timespec ts;if(clock_gettime(CLOCK_REALTIME,&ts))return -1;t->sec=ts.tv_sec;t->nsec=ts.tv_nsec;return 0;}
static int _time_cmp(const mq_time_t *a,const mq_time_t *b){if(a->sec!=b->sec)return a->sec<b->sec?-1:1;return (a->nsec>b->nsec)-(a->nsec<b->nsec);}
static int _parse_u64(const char *s,uint64_t *v){char *end;errno=0;unsigned long long x=strtoull(s,&end,10);if(errno||!*s||*end)return -1;*v=x;return 0;}
static int _parse_u32(const char *s,uint32_t *v){uint64_t n;if(_parse_u64(s,&n)||n>UINT32_MAX)return -1;*v=n;return 0;}
static int _field_safe(const char *s){return s&&*s&&!strchr(s,'|');}
static mq_active_record_t *_find_active_job(uint32_t id){return id==rec.job_id?&rec:NULL;}
static int _read_wal_line(FILE *f,char *buf,size_t size,size_t *len){if(!fgets(buf,size,f))return ferror(f)?-1:0;*len=strlen(buf);return *len&&buf[*len-1]=='\n'?1:-1;}
/* ACTUAL_READER */
int main(int argc,char **argv){
 int w,p[2],status;pid_t child;struct timespec ts;FILE *f;
 if(argc!=2||pipe(p))return 2;
 w=open(argv[1],O_WRONLY|O_APPEND);mq_nodeproof_fd=open(argv[1],O_RDONLY);
 if(w<0||mq_nodeproof_fd<0)return 3;
 rec.job_id=42;rec.uid=9;rec.sluid=77;rec.release_started=true;rec.final_state=32771;
 rec.final_start=1;rec.final_end=1;strcpy(rec.final_node,"node");
 child=fork();if(child<0)return 4;
 if(!child){close(p[0]);if(flock(w,LOCK_EX))_exit(5);if(write(p[1],"x",1)!=1)_exit(6);
  usleep(700000);if(clock_gettime(CLOCK_REALTIME,&ts))_exit(7);
  f=fdopen(w,"a");if(!f)_exit(8);
  fprintf(f,"P6NP1|42|77|9|32771|0|0|1|%" PRIu64 "|%u|node\n",(uint64_t)ts.tv_sec,(unsigned)ts.tv_nsec);
  if(fflush(f)||fsync(fileno(f)))_exit(9);flock(fileno(f),LOCK_UN);fclose(f);_exit(0);
 }
 close(p[1]);char c;if(read(p[0],&c,1)!=1)return 10;
 int rc=_find_node_proof(&rec);waitpid(child,&status,0);
 if(!WIFEXITED(status)||WEXITSTATUS(status)||rc!=1||!rec.proof_seen)return 11;
 return 0;
}
'''

class NodeProofLockRaceTest(unittest.TestCase):
    def test_writer_timestamps_after_reader_starts_waiting(self):
        source=os.environ.get('TIME26_TEST_SOURCE')
        if not source: self.skipTest('set TIME26_TEST_SOURCE to the assembled Slurm source')
        text=(Path(source)/'src/slurmctld/monthly_quota.c').read_text()
        start=text.index('static int _find_node_proof(')
        end=text.index('\nstatic int _replay_wal(',start)
        with tempfile.TemporaryDirectory(prefix='time26-proof-race-') as tmp:
            tmp=Path(tmp);src=tmp/'race.c';binary=tmp/'race';proof=tmp/'proof'
            src.write_text(HARNESS.replace('/* ACTUAL_READER */',text[start:end]))
            proof.write_bytes(b'P6NP1|HEADER|1\n')
            subprocess.run(['cc','-std=gnu11','-O2',str(src),'-o',str(binary)],check=True,capture_output=True)
            subprocess.run([str(binary),str(proof)],check=True,timeout=8)

if __name__=='__main__': unittest.main()
