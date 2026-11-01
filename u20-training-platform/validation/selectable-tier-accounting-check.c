#include "selectable-tier-accounting.h"
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
int main(void) {
 struct time_tier_balance b={{5,5,5},{0,0,0},0}, step=b;
 struct time_active_job jobs[]={{1,1,TIME_DEBUG,1},{2,1,TIME_IMPORTANT,1}}, single[2];
 memcpy(single,jobs,sizeof jobs);
 assert(time_tier_advance(&b,jobs,2,10)==0);
 assert(b.used[0]==5 && b.used[1]==5 && b.used[2]==5 && b.overage==5);
 for(int i=0;i<10;i++) assert(time_tier_advance(&step,single,2,1)==0);
 assert(memcmp(&b,&step,sizeof b)==0);
 assert(jobs[0].current==single[0].current && jobs[1].current==single[1].current);
 struct time_tier_balance chosen={{5,10,20},{0,0,0},0};
 struct time_active_job j={3,1,TIME_IMPORTANT,1};
 assert(time_tier_advance(&chosen,&j,1,12)==0);
 assert(chosen.used[0]==0 && chosen.used[1]==10 && chosen.used[2]==2 && j.current==TIME_NORMAL);
 struct time_tier_balance multi={{10,10,20},{0,0,0},0}, multi_step=multi;
 struct time_active_job parallel[]={{10,1,TIME_DEBUG,4},{11,1,TIME_IMPORTANT,1}}, divided[2];
 memcpy(divided,parallel,sizeof parallel);
 assert(time_tier_advance(&multi,parallel,2,3)==0);
 for(int i=0;i<3;i++) assert(time_tier_advance(&multi_step,divided,2,1)==0);
 assert(memcmp(&multi,&multi_step,sizeof multi)==0);
 assert(multi.used[TIME_DEBUG]==10 && multi.used[TIME_IMPORTANT]==5);
 struct time_tier_balance debt={{5,10,20},{5,10,20},7};
 assert(time_tier_add_normal(&debt,10)==0 && debt.used[2]==27 && debt.limit[2]==30 && debt.overage==0);
 struct time_tier_balance full={{5,10,20},{5,10,20},UINT64_MAX}, before=full;
 struct time_active_job over={4,1,TIME_NORMAL,1}, old=over;
 assert(time_tier_advance(&full,&over,1,1)==-1);
 assert(memcmp(&full,&before,sizeof full)==0 && over.current==old.current);
 struct time_active_job unsorted[]={{5,1,TIME_DEBUG,1},{4,1,TIME_DEBUG,1}};
 assert(time_tier_advance(&chosen,unsorted,2,1)==-1);
 puts("PASS: concurrent accounting, timestep consistency, tier choice, grant, overflow and identity ordering");
}
