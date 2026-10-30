/* Container-only helper for the isolated controller validation socket. */
#include <sys/socket.h>
#include <sys/un.h>
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
    time_t local_time;
    struct tm civil;
    if (access("/.dockerenv", F_OK) || !getenv("TIME_ISOLATED_VALIDATION") ||
        strcmp(getenv("TIME_ISOLATED_VALIDATION"), "1")) return 2;
    addr.sun_family = AF_UNIX;
    strcpy(addr.sun_path, "/tmp/time-isolated/state/time-quota.sock");
    fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0 || connect(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        perror("isolated quota socket"); return 1;
    }
    const char *request = argc == 2 && !strncmp(argv[1], "root", 4) ? "SNAP 0\n" : "SNAP 1001\n";
    size_t len = strlen(request);
    if (write(fd, request, len) != (ssize_t)len) { perror("snapshot write"); return 1; }
    count = read(fd, buf, sizeof(buf) - 1);
    if (count <= 0) { perror("snapshot read"); return 1; }
    buf[count] = '\0';
    fputs(buf, stdout);
    close(fd);
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
    return 0;
}
