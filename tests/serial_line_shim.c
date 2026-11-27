/* Linux-only test fixture: give two pseudo-terminals simulated modem lines.
 * Used only in a test-launched rigctld via LD_PRELOAD, never by the application.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include <pthread.h>

int ioctl(int fd, unsigned long request, ...) {
    va_list ap;
    va_start(ap, request);
    void *arg = va_arg(ap, void *);
    va_end(ap);
    int (*real_ioctl)(int, unsigned long, ...) = dlsym(RTLD_NEXT, "ioctl");
    if (request != TIOCMGET && request != TIOCMSET && request != TIOCMBIS && request != TIOCMBIC)
        return real_ioctl(fd, request, arg);
    char link[64], path[256];
    snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
    ssize_t size = readlink(link, path, sizeof(path)-1);
    if (size < 0) return real_ioctl(fd, request, arg);
    path[size] = 0;
    const char *ptt = getenv("DSC_TEST_PTT_TTY"), *cat = getenv("DSC_TEST_CAT_TTY");
    int index = ptt && !strcmp(path, ptt) ? 0 : cat && !strcmp(path, cat) ? 1 : -1;
    if (index < 0) return real_ioctl(fd, request, arg);
    static int bits[2];
    static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
    pthread_mutex_lock(&lock);
    if (request == TIOCMGET) *(int *)arg = bits[index];
    else {
        if (request == TIOCMSET) bits[index] = *(int *)arg;
        if (request == TIOCMBIS) bits[index] |= *(int *)arg;
        if (request == TIOCMBIC) bits[index] &= ~*(int *)arg;
        const char *log = getenv("DSC_TEST_PTT_LOG");
        FILE *out = log ? fopen(log, "a") : NULL;
        if (out) {
            fprintf(out, "%s %d %d\n", path, !!(bits[index] & TIOCM_RTS), !!(bits[index] & TIOCM_DTR));
            fclose(out);
        }
    }
    pthread_mutex_unlock(&lock);
    return 0;
}
