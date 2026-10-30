/* Container-only helper for the isolated controller validation socket. */
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(void)
{
    struct sockaddr_un addr = {0};
    char buf[1024];
    ssize_t count;
    int fd;
    if (access("/.dockerenv", F_OK) || !getenv("TIME_ISOLATED_VALIDATION") ||
        strcmp(getenv("TIME_ISOLATED_VALIDATION"), "1")) return 2;
    addr.sun_family = AF_UNIX;
    strcpy(addr.sun_path, "/tmp/time-isolated/state/time-quota.sock");
    fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0 || connect(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        perror("isolated quota socket"); return 1;
    }
    if (write(fd, "SNAP 1001\n", 10) != 10) { perror("snapshot write"); return 1; }
    count = read(fd, buf, sizeof(buf) - 1);
    if (count <= 0) { perror("snapshot read"); return 1; }
    buf[count] = '\0';
    fputs(buf, stdout);
    close(fd);
    return strncmp(buf, "OK ", 3) != 0;
}
