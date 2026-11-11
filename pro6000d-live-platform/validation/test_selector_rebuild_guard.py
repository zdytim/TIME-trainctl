"""Exercise the actual cons_tres GPU replay guard with synthetic lists."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HARNESS=r'''
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#define GRES_CONF_SHARED 1
#define GRES_STATE_TYPE_JOB 1
#define GRES_STATE_TYPE_NODE 2
typedef struct {int size;uint64_t bits;} bitstr_t;
typedef struct {int n;void **items;} list_t;
typedef struct {list_t *list;int pos;} list_itr_t;
typedef struct {uint32_t plugin_id;int state_type,config_flags;char *gres_name;void *gres_data;} gres_state_t;
typedef struct {char *type_name;unsigned flags,node_cnt;bitstr_t **gres_bit_alloc;uint64_t *gres_cnt_node_alloc;} gres_job_state_t;
typedef struct {bool no_consume;uint64_t gres_cnt_avail;bitstr_t *gres_bit_alloc;} gres_node_state_t;
static uint32_t gres_get_gpu_plugin_id(void){return 7;}
static int bit_set_count(bitstr_t *b){return __builtin_popcountll(b->bits);}
static int bit_size(bitstr_t *b){return b->size;}
static int bit_overlap_any(bitstr_t *a,bitstr_t *b){return !!(a->bits & b->bits);}
static list_itr_t *list_iterator_create(list_t *l){list_itr_t *i=calloc(1,sizeof(*i));i->list=l;return i;}
static void *list_next(list_itr_t *i){return i->pos<i->list->n?i->list->items[i->pos++]:NULL;}
static void list_iterator_destroy(list_itr_t *i){free(i);}
/* ACTUAL_GUARD */
int main(void){
 bitstr_t jobbits={4,2},nodebits={4,0};bitstr_t *bits[]={&jobbits};uint64_t counts[]={1};
 gres_job_state_t alloc={.node_cnt=1,.gres_bit_alloc=bits,.gres_cnt_node_alloc=counts};
 gres_node_state_t node={.gres_cnt_avail=4,.gres_bit_alloc=&nodebits};
 gres_state_t req={.plugin_id=7,.state_type=1,.gres_name="gpu",.gres_data=&alloc};
 gres_state_t j={.plugin_id=7,.state_type=1,.gres_name="gpu",.gres_data=&alloc};
 gres_state_t n={.plugin_id=7,.state_type=2,.gres_name="gpu",.gres_data=&node};
 void *req_items[]={&req},*job_items[]={&j},*node_items[]={&n};
 list_t r={1,req_items},a={1,job_items},d={1,node_items};
 if(_quota_check_rebuild_gpu(NULL,NULL,&d,0))return 1;
 if(!_quota_check_rebuild_gpu(&r,NULL,&d,0))return 2;
 if(_quota_check_rebuild_gpu(&r,&a,&d,0))return 3;
 nodebits.bits=2;if(!_quota_check_rebuild_gpu(&r,&a,&d,0))return 4;
 nodebits.bits=0;nodebits.size=3;if(!_quota_check_rebuild_gpu(&r,&a,&d,0))return 5;
 nodebits.size=4;counts[0]=2;if(!_quota_check_rebuild_gpu(&r,&a,&d,0))return 6;
 counts[0]=1;alloc.flags=GRES_CONF_SHARED;if(!_quota_check_rebuild_gpu(&r,&a,&d,0))return 7;
 alloc.flags=0;alloc.type_name="typed";if(_quota_check_rebuild_gpu(&r,&a,&d,0))return 8;
 return 0;
}
'''

class SelectorRebuildGuardTest(unittest.TestCase):
    def test_missing_duplicate_malformed_and_typed_gpu_allocations(self):
        source=os.environ.get('TIME26_TEST_SOURCE')
        if not source: self.skipTest('set TIME26_TEST_SOURCE to assembled Slurm source')
        text=(Path(source)/'src/plugins/select/cons_tres/job_resources.c').read_text()
        start=text.index('static int _quota_check_rebuild_gpu(')
        end=text.index('\n#endif',start)
        with tempfile.TemporaryDirectory(prefix='time26-selector-') as tmp:
            tmp=Path(tmp);src=tmp/'guard.c';binary=tmp/'guard'
            src.write_text(HARNESS.replace('/* ACTUAL_GUARD */',text[start:end]))
            subprocess.run(['cc','-std=gnu11','-O2',str(src),'-o',str(binary)],check=True,capture_output=True)
            subprocess.run([str(binary)],check=True,timeout=5)

if __name__=='__main__': unittest.main()
