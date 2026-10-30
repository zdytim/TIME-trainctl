/* Experiment only: substitute a private NVIDIA parameters file in an isolated
 * container. This is not a production access control or driver modification. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static const char *redirect(const char *path) {
    if (path && strcmp(path, "/proc/driver/nvidia/params") == 0) {
        static const char marker[] = "TIME_FIXTURE_PARAMS_INTERCEPTED\n";
        (void)write(2, marker, sizeof(marker) - 1);
        return "/tmp/time-fixture-params";
    }
    return path;
}

#define OPEN_WRAPPER(name) \
int name(const char *path, int flags, ...) { \
    mode_t mode = 0; \
    if (flags & O_CREAT) { va_list ap; va_start(ap, flags); mode = va_arg(ap, int); va_end(ap); } \
    int (*next)(const char *, int, ...) = dlsym(RTLD_NEXT, #name); \
    return next(redirect(path), flags, mode); \
}
#define OPENAT_WRAPPER(name) \
int name(int dirfd, const char *path, int flags, ...) { \
    mode_t mode = 0; \
    if (flags & O_CREAT) { va_list ap; va_start(ap, flags); mode = va_arg(ap, int); va_end(ap); } \
    int (*next)(int, const char *, int, ...) = dlsym(RTLD_NEXT, #name); \
    return next(dirfd, redirect(path), flags, mode); \
}
OPEN_WRAPPER(open)
OPEN_WRAPPER(open64)
OPENAT_WRAPPER(openat)
OPENAT_WRAPPER(openat64)

FILE *fopen(const char *path, const char *mode) {
    FILE *(*next)(const char *, const char *) = dlsym(RTLD_NEXT, "fopen");
    return next(redirect(path), mode);
}
FILE *fopen64(const char *path, const char *mode) {
    FILE *(*next)(const char *, const char *) = dlsym(RTLD_NEXT, "fopen64");
    return next(redirect(path), mode);
}
