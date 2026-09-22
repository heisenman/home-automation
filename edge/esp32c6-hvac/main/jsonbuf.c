#include "jsonbuf.h"

#include <stdarg.h>
#include <stdio.h>

void jsonbuf_init(jsonbuf_t *j, char *buf, size_t cap) {
    j->buf = buf;
    j->cap = cap;
    j->off = 0;
    j->any = false;
    if (cap >= 2) {
        buf[0] = '{';
        buf[1] = '\0';
        j->off = 1;
    }
}

void jsonbuf_add(jsonbuf_t *j, const char *fmt, ...) {
    if (j->off >= j->cap) return;

    // Snapshot BEFORE the separator so an overflowing field takes its own comma down with it.
    const size_t mark = j->off;
    if (j->any && j->off + 1 < j->cap) j->buf[j->off++] = ',';

    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(j->buf + j->off, j->cap - j->off, fmt, ap);
    va_end(ap);

    if (n < 0 || (size_t)n >= j->cap - j->off) {   // drop, never truncate
        j->off = mark;
        j->buf[j->off] = '\0';
        return;
    }
    j->off += (size_t)n;
    j->any = true;
}

bool jsonbuf_any(const jsonbuf_t *j) { return j->any; }

bool jsonbuf_finish(jsonbuf_t *j) {
    if (j->off + 2 > j->cap) return false;
    j->buf[j->off++] = '}';
    j->buf[j->off] = '\0';
    return true;
}
