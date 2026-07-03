// HTTP transport for the panel UI — extracted from ui_tiles.c (ADR-0020 module-first).
//
// Owns the BFF base URL (derived from the sensors URL by the orchestrator) and the three
// request shapes the UI needs. A leaf apart from secrets.h (PANEL_TOKEN). Callers building
// /devices|/control|/auth URLs borrow the base via ui_http_base().
#pragma once
#include "esp_http_client.h"

// Set the BFF base URL (http://host:port). Called once by the orchestrator at start.
void ui_http_init(const char *base);
// Borrow the BFF base URL for building request URLs (never NULL after ui_http_init).
const char *ui_http_base(void);

// GET url into a freshly heap_caps_malloc'd PSRAM buffer (NUL-terminated); caller heap_caps_free's.
// *out_len = bytes read. Returns NULL on alloc/connect failure.
char *ui_http_get(const char *url, int *out_len);

// POST body to url with the operator token (Authorization: Bearer PANEL_TOKEN). Returns HTTP status (or -1).
int ui_http_post_cmd(const char *url, const char *body);

// Send body via method to url with an optional bearer; capture up to resp_cap-1 bytes of the
// response body. Returns HTTP status (or -1). Used for admin login/scene/policy.
int ui_http_send_json(esp_http_client_method_t method, const char *url, const char *body,
                      const char *bearer, char *resp, int resp_cap);
