// aprilaire_bus — see aprilaire_bus.h.
#include "aprilaire_bus.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "ha_aprilaire.h"
#include "ha_rs485.h"

static const char *TAG = "apr_bus";

// Pins: docs/edge-pinouts.md + design §"Node builds" (same as hvac_c6). NEVER D6/D7 (UART0 console).
#define BUS_UART        UART_NUM_1
#define BUS_TX_GPIO     18          // D10
#define BUS_RX_GPIO     20          // D9
#define BUS_BAUD        9600
#define LINK_MS         5000u       // M-frame within this window = link up
#define REPORT_MS       300000u     // periodic diagnostic report
#define RAW_RING        4           // last raw bodies kept for the report

#define DRY_CALL        7           // 40 °F dew point
#define DRY_IDLE        1           // 65 °F — and the 1 -> 7 step on a call forces an immediate sample
#define REMOTE_RH_X10   500         // what a Model 76 would show as its own RH; not used for the run decision

static ha_rs485_t   s_bus;
static void       (*s_log)(const char *fmt, ...);
static SemaphoreHandle_t s_mu;
static volatile bool s_call;
static apr_bus_status_t s_st;
static uint32_t     s_last_m_ms;
static uint8_t      s_raw[RAW_RING][HA_APR_MAX_BODY + 1];
static unsigned     s_raw_n;
static volatile int s_tx_req_secs;
static volatile uint8_t s_tx_req_byte;

static uint32_t now_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }

static ha_rs485_cfg_t bus_cfg(void) {
    // listen_only = false: this node is the unit's remote. It writes ONLY in reply to a valid M-frame, so an
    // EXTERNAL-mode (silent) bus is never driven. The bench txtest is the one other writer, and it is refused
    // while the link is live.
    return (ha_rs485_cfg_t){
        .port = BUS_UART, .tx_gpio = BUS_TX_GPIO, .rx_gpio = BUS_RX_GPIO, .de_gpio = -1,
        .baud = BUS_BAUD, .data_bits = UART_DATA_8_BITS, .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1, .listen_only = false };
}

void apr_bus_set_call(bool on) { s_call = on; }

void apr_bus_get_status(apr_bus_status_t *out) {
    if (!s_mu) { memset(out, 0, sizeof *out); return; }   // called before apr_bus_start (dh_task starts first)
    xSemaphoreTake(s_mu, portMAX_DELAY);
    *out = s_st;
    out->link = s_st.m_ok && (uint32_t)(now_ms() - s_last_m_ms) < LINK_MS;
    xSemaphoreGive(s_mu);
}

void apr_bus_report(void) {
    if (!s_log) return;
    apr_bus_status_t st;
    apr_bus_get_status(&st);
    const char *verdict =
        st.link ? (st.code ? "LINK UP — unit reports an error code (see manual Table 2)" : "LINK UP — answering as the unit's remote")
        : st.m_ok ? "LINK LOST — no M-frame for >5 s (REMOTE switched off? wiring?)"
                  : "NO M-FRAMES — unit in EXTERNAL mode (DH relay controls it) or A/B not wired";
    s_log("apr-bus: %s | call=%d unit=%s rh=%u%% code=%u | M ok=%lu bad=%lu R sent=%lu",
          verdict, s_call ? 1 : 0, st.running ? "RUNNING" : "idle", st.rh_pct, st.code,
          (unsigned long)st.m_ok, (unsigned long)st.m_bad, (unsigned long)st.r_sent);
    xSemaphoreTake(s_mu, portMAX_DELAY);
    unsigned n = s_raw_n, shown = n < RAW_RING ? n : RAW_RING;
    char raw[RAW_RING][HA_APR_MAX_BODY + 1];
    memcpy(raw, s_raw, sizeof raw);
    xSemaphoreGive(s_mu);
    for (unsigned i = 0; i < shown; i++) s_log("apr-bus: raw[-%u] %s", shown - i, raw[(n - shown + i) % RAW_RING]);
}

static void on_body(const uint8_t *body, size_t len) {
    ha_apr_m_t m;
    bool ok = ha_apr_parse_m(body, len, &m);
    xSemaphoreTake(s_mu, portMAX_DELAY);
    size_t k = len < HA_APR_MAX_BODY ? len : HA_APR_MAX_BODY;
    for (size_t i = 0; i < k; i++) s_raw[s_raw_n % RAW_RING][i] = (body[i] >= 0x20 && body[i] < 0x7F) ? body[i] : '.';
    s_raw[s_raw_n % RAW_RING][k] = 0;
    s_raw_n++;
    if (ok) {
        bool changed = s_st.m_ok == 0 || m.running != s_st.running || m.code != s_st.code;
        s_st.m_ok++;
        s_st.running = m.running;
        s_st.rh_pct = m.rh_pct;
        s_st.code = m.code;
        s_last_m_ms = now_ms();
        xSemaphoreGive(s_mu);
        if (changed && s_log)
            s_log("apr-bus: unit %s rh=%u%% code=%u (call=%d)", m.running ? "RUNNING" : "idle", m.rh_pct, m.code,
                  s_call ? 1 : 0);
        bool call = s_call;
        uint8_t frame[24];
        size_t n = ha_apr_build_r(&(ha_apr_r_t){ .on = call, .dryness = call ? DRY_CALL : DRY_IDLE,
                                                 .rh_x10 = REMOTE_RH_X10 }, frame, sizeof frame);
        if (n && ha_rs485_write(&s_bus, frame, n, 100) == ESP_OK) {
            xSemaphoreTake(s_mu, portMAX_DELAY);
            s_st.r_sent++;
            xSemaphoreGive(s_mu);
        }
    } else {
        s_st.m_bad++;
        xSemaphoreGive(s_mu);
    }
}

static void run_txtest(int secs, uint8_t pat) {
    s_log("apr-txtest: streaming 0x%02X for %d s (bench: watch A-B on a meter)", pat, secs);
    uint8_t burst[32];
    memset(burst, pat, sizeof burst);
    uint64_t sent = 0;
    uint32_t end = now_ms() + (uint32_t)secs * 1000u;
    while ((int32_t)(end - now_ms()) > 0)
        if (ha_rs485_write(&s_bus, burst, sizeof burst, 200) == ESP_OK) sent += sizeof burst;
    ha_rs485_flush_input(&s_bus);
    s_log("apr-txtest: sent=%llu B — done", (unsigned long long)sent);
}

const char *apr_bus_txtest(int secs, int byte) {
    if (!s_bus.inited) return "REFUSED (bus not running)";
    if (secs < 1 || secs > 30 || byte < 0 || byte > 255) return "REFUSED (secs 1..30, byte 0..255)";
    apr_bus_status_t st;
    apr_bus_get_status(&st);
    if (st.link) return "REFUSED (link live — the unit is on this bus; bench-only test)";
    s_tx_req_byte = (uint8_t)byte;
    s_tx_req_secs = secs;
    return "queued";
}

static void bus_task(void *arg) {
    (void)arg;
    ha_apr_framer_t fr = {0};
    uint8_t rx[32];
    uint32_t last_report = now_ms();
    bool was_link = false;
    for (;;) {
        if (s_tx_req_secs) {
            int secs = s_tx_req_secs;
            s_tx_req_secs = 0;
            run_txtest(secs, s_tx_req_byte);
            memset(&fr, 0, sizeof fr);
        }
        int n = ha_rs485_read(&s_bus, rx, sizeof rx, 5);
        for (int i = 0; i < n; i++)
            if (ha_apr_feed(&fr, rx[i])) on_body(fr.body, fr.len);
        apr_bus_status_t st;
        apr_bus_get_status(&st);
        if (st.link != was_link && s_log) {
            s_log("apr-bus: link %s", st.link ? "UP — unit in REMOTE mode, answering" : "DOWN — no M-frames (EXTERNAL mode or wiring)");
            was_link = st.link;
        }
        if ((uint32_t)(now_ms() - last_report) >= REPORT_MS) {
            apr_bus_report();
            last_report = now_ms();
        }
    }
}

bool apr_bus_start(void (*log)(const char *fmt, ...)) {
    s_log = log;
    s_mu = xSemaphoreCreateMutex();
    ha_rs485_cfg_t cfg = bus_cfg();
    esp_err_t err = ha_rs485_init(&s_bus, &cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ha_rs485_init failed: %s", esp_err_to_name(err));
        return false;
    }
    uart_set_rx_timeout(BUS_UART, 2);   // hand bytes over ~2 char times after the line idles (prompt replies)
    xTaskCreate(bus_task, "apr_bus", 4096, NULL, 6, NULL);
    ESP_LOGW(TAG, "Aprilaire Remote bus up: UART1 TX=GPIO%d RX=GPIO%d @%d 8N1 — answers M-frames",
             BUS_TX_GPIO, BUS_RX_GPIO, BUS_BAUD);
    return true;
}
