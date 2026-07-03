// HTTP transport for the panel UI (see ui_http.h). Extracted from ui_tiles.c verbatim
// (ADR-0020 module-first); behavior identical. The BFF base URL lives here now; workers
// read it via ui_http_base() rather than a shared static.
#include "ui/ui_http.h"
#include <stdio.h>
#include <string.h>
#include "esp_heap_caps.h"
#include "secrets.h"   // PANEL_TOKEN (operator JWT) for command POSTs

static char s_base[192];   // BFF base URL (http://host:port), derived from the sensors URL

void ui_http_init(const char *base) { snprintf(s_base, sizeof(s_base), "%s", base); }
const char *ui_http_base(void) { return s_base; }

// ---- HTTP GET into a PSRAM buffer (caller frees) ----
char *ui_http_get(const char *url, int *out_len)
{
    esp_http_client_config_t cfg = { .url = url, .timeout_ms = 8000 };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) return NULL;
    char *buf = NULL; int total = 0;
    if (esp_http_client_open(c, 0) == ESP_OK) {
        int cl = esp_http_client_fetch_headers(c);
        int cap = (cl > 0) ? cl + 1 : 32768;
        buf = heap_caps_malloc(cap, MALLOC_CAP_SPIRAM);
        if (buf) {
            int r;
            while (total < cap - 1 &&
                   (r = esp_http_client_read(c, buf + total, cap - 1 - total)) > 0)
                total += r;
            buf[total] = 0;
        }
    }
    esp_http_client_close(c);
    esp_http_client_cleanup(c);
    *out_len = total;
    return buf;
}

// POST a command to the BFF control API with the operator token. Returns HTTP
// status (or -1). Runs on cmd_worker, never on the LVGL/click stack.
int ui_http_post_cmd(const char *url, const char *body)
{
    esp_http_client_config_t cfg = { .url = url, .timeout_ms = 8000, .method = HTTP_METHOD_POST };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) return -1;
    esp_http_client_set_header(c, "Content-Type", "application/json");
    esp_http_client_set_header(c, "Authorization", "Bearer " PANEL_TOKEN);
    esp_http_client_set_post_field(c, body, strlen(body));
    int code = -1;
    if (esp_http_client_perform(c) == ESP_OK) code = esp_http_client_get_status_code(c);
    esp_http_client_cleanup(c);
    return code;
}

// Send JSON (POST/PUT) with an optional bearer; capture the response body. Runs on admin_worker only.
int ui_http_send_json(esp_http_client_method_t method, const char *url, const char *body,
                      const char *bearer, char *resp, int resp_cap)
{
    esp_http_client_config_t cfg = { .url = url, .timeout_ms = 8000, .method = method };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) return -1;
    esp_http_client_set_header(c, "Content-Type", "application/json");
    if (bearer && bearer[0]) {
        char h[440];
        snprintf(h, sizeof(h), "Bearer %s", bearer);
        esp_http_client_set_header(c, "Authorization", h);
    }
    if (resp && resp_cap > 0) resp[0] = 0;
    int code = -1, blen = strlen(body);
    if (esp_http_client_open(c, blen) == ESP_OK) {
        esp_http_client_write(c, body, blen);
        esp_http_client_fetch_headers(c);
        code = esp_http_client_get_status_code(c);
        if (resp && resp_cap > 1) {
            int n = esp_http_client_read_response(c, resp, resp_cap - 1);
            if (n >= 0) resp[n] = 0;
        }
    }
    esp_http_client_close(c);
    esp_http_client_cleanup(c);
    return code;
}
