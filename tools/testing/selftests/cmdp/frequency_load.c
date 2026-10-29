// SPDX-License-Identifier: GPL-2.0
// Scalar integer all-core qualification load. No AVX and no changing host policy.
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
static volatile sig_atomic_t stopped;
static volatile uint64_t sink;
static void halt(int sig) { (void)sig; stopped=1; }
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec+t.tv_nsec/1e9; }
static int worker(int cpu, double seconds) {
 cpu_set_t set; CPU_ZERO(&set); CPU_SET(cpu,&set);
 if(sched_setaffinity(0,sizeof(set),&set)) {perror("sched_setaffinity");return 2;}
 if(prctl(PR_SET_PDEATHSIG,SIGTERM)) {perror("PR_SET_PDEATHSIG");return 2;}
 if(getppid()==1) return 2;
 printf("READY cpu=%d pid=%ld\n",cpu,(long)getpid()); fflush(stdout);
 uint64_t a=UINT64_C(0x9e3779b97f4a7c15)+(unsigned)cpu,b=17,c=31,d=43;
 const double end=now()+seconds;
 while(!stopped && now()<end) {
  for(unsigned i=0;i<65536;i++) {
   a=(a^(a>>11))*UINT64_C(0xbf58476d1ce4e5b9); b=(b^(b<<13))+a;
   c=(c^(c>>7))*UINT64_C(0x94d049bb133111eb); d=(d^(d<<17))+c+b;
   __asm__ __volatile__("" : "+r"(a),"+r"(b),"+r"(c),"+r"(d));
  }
  sink=a+b+c+d;
 }
 printf("STOP cpu=%d checksum=%" PRIu64 "\n",cpu,(uint64_t)sink); fflush(stdout); return 0;
}
int main(int argc,char **argv) {
 if(argc<3) {fprintf(stderr,"usage: %s SECONDS CPU [CPU ...]\n",argv[0]);return 2;}
 char *end; double seconds=strtod(argv[1],&end);
 if(*end||seconds<=0||seconds>86400) return 2;
 signal(SIGTERM,halt);signal(SIGINT,halt);signal(SIGHUP,halt);
 pid_t *children=calloc((size_t)argc,sizeof(*children));if(!children)return 2;
 int n=0,rc=0;
 for(int i=2;i<argc;i++) {
  long cpu=strtol(argv[i],&end,10);if(*end||cpu<0||cpu>=CPU_SETSIZE){rc=2;break;}
  pid_t p=fork();if(p<0){perror("fork");rc=2;break;}
  if(!p) _exit(worker((int)cpu,seconds));
  children[n++]=p;
 }
 while(n>0) {
  if(stopped||rc) for(int i=0;i<n;i++) if(children[i]>0) kill(children[i],SIGTERM);
  int status; pid_t p=wait(&status);
  if(p<0) {if(errno==EINTR)continue;if(errno==ECHILD)break;perror("wait");rc=2;break;}
  if(!WIFEXITED(status)||WEXITSTATUS(status)) rc=2;
  for(int i=0;i<n;i++)if(children[i]==p){children[i]=children[n-1];n--;break;}
 }
 free(children);return rc;
}
