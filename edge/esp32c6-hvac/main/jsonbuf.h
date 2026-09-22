// Tiny append-only JSON object builder for node telemetry payloads.
//
// Pure — no ESP deps — so it is proven on the host (`test/run.sh`) rather than on the wire. It lives in
// its own translation unit for exactly that reason: the first version of this was inline in app_main.c,
// shipped two defects, and both of them produce a payload the writer silently drops rather than an
// error anyone would notice.
//
// ⚠️ THE BUILDER OWNS THE ENCLOSING BRACES. ha_mqtt_publish_node_sensor() takes the inner object
// *including* `{}`. Emitting bare `"k":v` pairs yields `"metrics":"online":false,...` on the wire —
// unparseable. (Bench, 2026-09-22, first publish.)
#pragma once
#include <stdbool.h>
#include <stddef.h>

typedef struct {
    char  *buf;
    size_t cap;
    size_t off;
    bool   any;    // true once at least one field has been accepted
} jsonbuf_t;

// Starts an object: the buffer becomes "{". Needs cap >= 2.
void jsonbuf_init(jsonbuf_t *j, char *buf, size_t cap);

// Appends `"key":value` formatted per `fmt`, inserting a separating comma when needed.
//
// A field that would not fit is dropped WHOLE — including the comma already written for it. Truncating
// mid-field, or leaving `{"a":1,}`, would cost the entire payload rather than the one field that
// overflowed. Callers do not need to check: an overflowed field is simply absent.
void jsonbuf_add(jsonbuf_t *j, const char *fmt, ...) __attribute__((format(printf, 2, 3)));

// True if at least one field was accepted — i.e. there is something worth publishing.
bool jsonbuf_any(const jsonbuf_t *j);

// Closes the object. False if the closing brace would not fit, in which case the caller must NOT
// publish: a truncated payload is worse than a missing one.
bool jsonbuf_finish(jsonbuf_t *j);
