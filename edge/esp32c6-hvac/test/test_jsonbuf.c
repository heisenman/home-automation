// Host unit test for the telemetry JSON builder (edge/esp32c6-hvac/main/jsonbuf.c).
//
// This exists because the first version of this builder shipped two defects, and BOTH of them produce
// a payload the writer silently drops rather than an error anyone would notice:
//   1. no enclosing braces -> `"metrics":"online":false,...` on the wire
//   2. an overflowing field left its comma behind -> `{"a":1,}`
// Caught on the bench 2026-09-22 at the cost of a flash cycle. Cheaper here.
#include <stdio.h>
#include <string.h>

#include "jsonbuf.h"

static int fails = 0;

static void check(const char *name, const char *got, const char *want) {
    int ok = strcmp(got, want) == 0;
    if (!ok) fails++;
    printf("%-46s %s\n", name, ok ? "PASS" : "FAIL");
    if (!ok) printf("     got  %s\n     want %s\n", got, want);
}

static void check_true(const char *name, int cond) {
    if (!cond) fails++;
    printf("%-46s %s\n", name, cond ? "PASS" : "FAIL");
}

int main(void) {
    char b[256];
    jsonbuf_t j;

    // ── the shape ha_mqtt_publish_node_sensor expects: inner object WITH braces ──
    jsonbuf_init(&j, b, sizeof b);
    jsonbuf_add(&j, "\"a\":%d", 1);
    jsonbuf_add(&j, "\"b\":%s", "true");
    check_true("finish succeeds", jsonbuf_finish(&j));
    check("two fields form a valid object", b, "{\"a\":1,\"b\":true}");

    jsonbuf_init(&j, b, sizeof b);
    jsonbuf_add(&j, "\"only\":%d", 7);
    jsonbuf_finish(&j);
    check("single field has no stray comma", b, "{\"only\":7}");

    jsonbuf_init(&j, b, sizeof b);
    check_true("empty object reports nothing added", !jsonbuf_any(&j));
    jsonbuf_finish(&j);
    check("empty object still closes", b, "{}");

    jsonbuf_init(&j, b, sizeof b);
    jsonbuf_add(&j, "\"x\":%d", 1);
    check_true("any() true after a field", jsonbuf_any(&j));

    // ── floats format as JSON numbers, not locale junk ──
    jsonbuf_init(&j, b, sizeof b);
    jsonbuf_add(&j, "\"p\":%.2f", 12.5);
    jsonbuf_finish(&j);
    check("float field", b, "{\"p\":12.50}");

    // ── overflow: the field that does not fit vanishes WITH its comma ──
    char small[20];
    jsonbuf_init(&j, small, sizeof small);
    jsonbuf_add(&j, "\"aa\":%d", 11);
    jsonbuf_add(&j, "\"bbbbbbbbbbbbbb\":%d", 22);      // cannot fit
    check_true("overflow still closes", jsonbuf_finish(&j));
    check("overflowed field drops with its comma", small, "{\"aa\":11}");

    // An overflow on the FIRST field must leave a valid empty object, and any() must stay false so the
    // caller skips publishing instead of emitting a contentless reading.
    jsonbuf_init(&j, small, sizeof small);
    jsonbuf_add(&j, "\"wayyyyyyyyyy_too_long\":%d", 1);
    check_true("first-field overflow leaves any()==false", !jsonbuf_any(&j));
    jsonbuf_finish(&j);
    check("first-field overflow leaves {}", small, "{}");

    // ── a buffer too small even for "{}" must refuse rather than scribble ──
    char tiny[2];
    jsonbuf_init(&j, tiny, sizeof tiny);
    check_true("cap==2 cannot close (needs 3 with NUL)", !jsonbuf_finish(&j));

    char tiny3[3];
    jsonbuf_init(&j, tiny3, sizeof tiny3);
    check_true("cap==3 closes", jsonbuf_finish(&j));
    check("cap==3 gives {}", tiny3, "{}");

    printf("\n%s (%d failure(s))\n", fails ? "FAILURES" : "ALL PASS", fails);
    return fails != 0;
}
