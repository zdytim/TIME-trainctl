/* Test-only LD_PRELOAD shim. The fixture confines this to a GPU-free VM. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

int clock_gettime(clockid_t id, struct timespec *out)
{
    static int (*real_clock)(clockid_t, struct timespec *);
    const char *path;
    char buf[64];
    ssize_t n;
    int fd, rc;
    if (!real_clock) real_clock = dlsym(RTLD_NEXT, "clock_gettime");
    if (!real_clock) return -1;
    rc = real_clock(id, out);
    path = getenv("TIME_FAKE_REALTIME_OFFSET_FILE");
    if (rc || id != CLOCK_REALTIME || !path) return rc;
    fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -1;
    n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0) return -1;
    buf[n] = 0;
    out->tv_sec += strtoll(buf, NULL, 10);
    return rc;
}
