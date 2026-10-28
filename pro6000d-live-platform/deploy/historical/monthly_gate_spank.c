#error "SUPERSEDED: user requires strict pre-allocation gating; do not build or deploy this prototype"
/* Historical prototype: payload-execution gate, NOT a Slurm allocation/scheduler gate.
 * Build against each installed Slurm's headers; enable only after the documented
 * prerequisites. No environment variable or user option controls enforcement.
 */
#define _GNU_SOURCE
#include <slurm/spank.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <poll.h>
#include <time.h>
#include <unistd.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>

SPANK_PLUGIN(pro6000d_monthly_gate, 1);

static const char gate_dir[] = "/run/pro6000d-training";
static const char gate_socket[] = "/run/pro6000d-training/gate.sock";

static int64_t millis(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts)) return -1;
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static int ready(int fd, short events, int64_t deadline)
{
    struct pollfd p = {.fd=fd, .events=events};
    for (;;) {
        int64_t now = millis();
        if (now < 0 || now >= deadline) return -1;
        int rc = poll(&p, 1, (int)(deadline - now));
        if (rc < 0 && errno == EINTR) continue;
        if (rc <= 0 || (p.revents & (POLLERR | POLLNVAL))) return -1;
        return (p.revents & events) ? 0 : -1;
    }
}

static int ask(const char *request, const char *expected)
{
    struct stat st;
    struct sockaddr_un addr = {.sun_family=AF_UNIX};
    struct ucred peer;
    socklen_t size = sizeof(peer);
    int fd = -1, rc = -1, error = 0;
    int64_t deadline = millis();
    char reply[256];
    size_t sent = 0, received = 0, length = strlen(request);
    if (deadline < 0) return -1;
    deadline += 10000;
    /* The fixed parent and socket must not be replaceable by training users. */
    if (lstat(gate_dir, &st) || !S_ISDIR(st.st_mode) || st.st_uid != 0 ||
        (st.st_mode & 0022)) return -1;
    if (lstat(gate_socket, &st) || !S_ISSOCK(st.st_mode) || st.st_uid != 0 ||
        (st.st_mode & 0077)) return -1;
    fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (fd < 0) return -1;
    memcpy(addr.sun_path, gate_socket, sizeof(gate_socket));
    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr))) {
        if (errno != EINPROGRESS) goto done;
        if (ready(fd, POLLOUT, deadline)) goto done;
        size = sizeof(error);
        if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &size) || error) goto done;
    }
    size = sizeof(peer);
    if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &peer, &size) ||
        size != sizeof(peer) || peer.uid != 0) goto done;
    while (sent < length) {
        if (ready(fd, POLLOUT, deadline)) goto done;
        ssize_t n = send(fd, request + sent, length - sent, MSG_NOSIGNAL);
        if (n < 0 && (errno == EINTR || errno == EAGAIN)) continue;
        if (n <= 0) goto done;
        sent += (size_t)n;
    }
    /* Require EOF after exactly one echoed ALLOW frame; no prefix matching. */
    while (received < sizeof(reply) - 1) {
        struct pollfd p = {.fd=fd, .events=POLLIN};
        int64_t now = millis();
        if (now < 0 || now >= deadline) goto done;
        int polled = poll(&p, 1, (int)(deadline-now));
        if (polled < 0 && errno == EINTR) continue;
        if (polled <= 0 || (p.revents & (POLLERR | POLLNVAL))) goto done;
        ssize_t n = recv(fd, reply + received, sizeof(reply)-1-received, 0);
        if (n < 0 && (errno == EINTR || errno == EAGAIN)) continue;
        if (n < 0) goto done;
        if (!n) {
            reply[received] = '\0';
            rc = received == strlen(expected) &&
                 !memcmp(reply, expected, received) ? 0 : -1;
            goto done;
        }
        received += (size_t)n;
    }
done:
    close(fd);
    return rc;
}

int slurm_spank_task_init_privileged(spank_t sp, int ac, char **av)
{
    uid_t uid;
    uint32_t job, step, restart;
    char request[192], expected[208];
    (void)av;
    /* There are deliberately no bypass or audit-only plugin arguments. */
    if (ac || spank_remote(sp) != 1 || geteuid() != 0 ||
        spank_get_item(sp, S_JOB_UID, &uid) != ESPANK_SUCCESS ||
        spank_get_item(sp, S_JOB_ID, &job) != ESPANK_SUCCESS ||
        spank_get_item(sp, S_JOB_STEPID, &step) != ESPANK_SUCCESS ||
        spank_get_item(sp, S_SLURM_RESTART_COUNT, &restart) != ESPANK_SUCCESS) {
        slurm_error("monthly gate: trusted task identity unavailable; execution denied");
        return -1;
    }
    snprintf(request, sizeof(request), "P6G1 %u %u %u %u\n",
             job, step, (unsigned)uid, restart);
    snprintf(expected, sizeof(expected), "ALLOW %s", request);
    if (ask(request, expected)) {
        slurm_error("monthly gate: job %u step %u lacks an execution permit", job, step);
        return -1;
    }
    return 0;
}
