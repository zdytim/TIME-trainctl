/* Container-only helper for the isolated controller validation socket. */
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

int main(int argc, char **argv)
{
    struct sockaddr_un addr = {0};
    char buf[1024];
    ssize_t count;
    int fd;
    unsigned long long fields[17];
    char *cursor, *end;
    char grant_request[256];
    time_t local_time;
    struct tm civil;
    if (access("/.dockerenv", F_OK) || !getenv("TIME_ISOLATED_VALIDATION") ||
        strcmp(getenv("TIME_ISOLATED_VALIDATION"), "1")) return 2;
    addr.sun_family = AF_UNIX;
    strcpy(addr.sun_path, "/tmp/time-isolated/state/time-quota.sock");
    fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (argc == 2 && !strcmp(argv[1], "listen")) {
        if (fd < 0) return 1;
        umask(0077);
        if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) || listen(fd, 4)) return 1;
        for (;;) {
            int peer = accept(fd, NULL, NULL);
            if (peer < 0) return 1;
            close(peer);
        }
    }
    if (fd < 0 || connect(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        perror("isolated quota socket"); return 1;
    }
    const char *request = argc == 2 && !strncmp(argv[1], "root", 4) ? "SNAP 0\n" : "SNAP 1001\n";
    if (argc == 4 && (!strcmp(argv[1], "grant") || !strcmp(argv[1], "grant-conflict"))) {
        time_t now = time(NULL) + 8 * 60 * 60;
        struct tm current;
        if (!gmtime_r(&now, &current) || strspn(argv[2], "0123456789") != strlen(argv[2]) ||
            strspn(argv[3], "abcdefghijklmnopqrstuvwxyz0123456789-") != strlen(argv[3])) return 2;
        int month = (current.tm_year + 1900) * 100 + current.tm_mon + 1;
        int written = snprintf(grant_request, sizeof(grant_request), "GRANT 0 %d %s %s validator\n", month, argv[2], argv[3]);
        if (written < 0 || (size_t)written >= sizeof(grant_request)) return 2;
        request = grant_request;
    }
    size_t len = strlen(request);
    if (write(fd, request, len) != (ssize_t)len) { perror("snapshot write"); return 1; }
    count = read(fd, buf, sizeof(buf) - 1);
    if (count <= 0) { perror("snapshot read"); return 1; }
    buf[count] = '\0';
    fputs(buf, stdout);
    close(fd);
    if (argc == 4 && !strcmp(argv[1], "grant")) return strcmp(buf, "OK\n") != 0;
    if (argc == 4 && !strcmp(argv[1], "grant-conflict")) return strcmp(buf, "ERROR unavailable_or_invalid\n") != 0;
    if (argc == 2 && !strcmp(argv[1], "root-unavailable")) return strcmp(buf, "ERROR unavailable_or_invalid\n") != 0;
    if (strncmp(buf, "OK ", 3)) return 1;
    cursor = buf + 3;
    for (int i = 0; i < 17; i++) {
        fields[i] = strtoull(cursor, &end, 10);
        if (end == cursor || (*end != ' ' && *end != '\n')) return 1;
        cursor = end + 1;
    }
    local_time = (time_t)fields[14] + 8 * 60 * 60;
    if (!gmtime_r(&local_time, &civil)) return 1;
    if (fields[0] != (unsigned long long)((civil.tm_year + 1900) * 100 + civil.tm_mon + 1)) {
        fprintf(stderr, "quota month does not match snapshot timestamp\n");
        return 1;
    }
    if (argc == 2 && !strcmp(argv[1], "root") && fields[1] != 3) {
        fprintf(stderr, "synthetic user is not exhausted\n"); return 1;
    }
    if (argc == 2 && !strcmp(argv[1], "root-eligible") && fields[1] != 0) {
        fprintf(stderr, "synthetic user is not in debug tier\n"); return 1;
    }
    unsigned long long expected_gpus = 1;
    if (getenv("TIME_EXPECT_GPUS")) expected_gpus = strtoull(getenv("TIME_EXPECT_GPUS"), NULL, 10);
    if (argc == 2 && !strcmp(argv[1], "root-active") &&
        (fields[12] != expected_gpus || fields[13] != 1 || fields[2] < 1)) {
        fprintf(stderr, "running allocation or elapsed usage is not accounted for\n"); return 1;
    }
    if (argc == 2 && !strcmp(argv[1], "root-active-exhausted") &&
        (fields[1] != 3 || fields[12] != expected_gpus || fields[13] != 1 ||
         (fields[10] == 0 && fields[11] == 0))) {
        fprintf(stderr, "running exhausted job or overage is not accounted for\n"); return 1;
    }
    if (argc == 2 && !strcmp(argv[1], "root-finished-overage") &&
        (fields[1] != 3 || fields[12] != 0 || fields[13] != 0 ||
         (fields[10] == 0 && fields[11] == 0))) {
        fprintf(stderr, "finished job or its overage was not reconciled\n"); return 1;
    }
    if (argc == 2 && !strcmp(argv[1], "root-tier")) {
        const char *value = getenv("TIME_EXPECT_TIER");
        if (!value || fields[1] != strtoull(value, NULL, 10)) {
            fprintf(stderr, "synthetic user has an unexpected tier\n"); return 1;
        }
    }
    if (argc == 2 && !strcmp(argv[1], "root-overage") &&
        (fields[1] != 3 || fields[4] != 20 || fields[10] != 20 || fields[11] != 0)) return 1;
    if (argc == 2 && !strcmp(argv[1], "root-granted") &&
        (fields[1] != 2 || fields[4] != 60 || fields[8] != 20 || fields[9] != 0)) return 1;
    return 0;
}
