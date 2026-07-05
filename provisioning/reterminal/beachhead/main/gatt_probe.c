// GATT-central connect+discover probe — see gatt_probe.h. Spike 0 for roadmap #5: does the C6 controller
// support the CENTRAL role (initiating a connection) over the esp_hosted HCI, or only observing? The
// connect/GAP/discovery flow is lifted verbatim-in-shape from the live edge verifier gatt_history.c so a
// success here means the full history port is mechanical.
#include "gatt_probe.h"
#include "ha_ble_scan.h"      // pause/resume + addr lookup + own_addr_type (shared component)
#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "host/ble_hs.h"
#include "host/ble_gap.h"
#include "host/ble_gatt.h"

static const char *TAG = "gatt_probe";
#define BIT_CONNECTED  (1 << 0)
#define BIT_FAIL       (1 << 1)
#define BIT_DISCONNECT (1 << 2)
#define BIT_DISC_DONE  (1 << 3)

static volatile bool s_busy;
static EventGroupHandle_t s_evt;
static uint16_t s_conn;
static int s_nsvc;
static char s_mac[18];
static void (*s_report)(const char *line);

void gatt_probe_set_reporter(void (*fn)(const char *line)) { s_report = fn; }

static void report(const char *fmt, ...)
{
    char b[160];
    va_list ap; va_start(ap, fmt); vsnprintf(b, sizeof(b), fmt, ap); va_end(ap);
    ESP_LOGI(TAG, "%s", b);
    if (s_report) s_report(b);
}

// Same fast params the SwitchBot pull uses (edge gatt_history.c) — keeps the link snappy + stable.
static const struct ble_gap_conn_params CONN = {
    .scan_itvl = 0x0010, .scan_window = 0x0010,
    .itvl_min = 24, .itvl_max = 40,            // 30..50 ms connection interval
    .latency = 0, .supervision_timeout = 400,  // 4 s
    .min_ce_len = 0, .max_ce_len = 0,
};

// Service-discovery callback: log each service; on BLE_HS_EDONE the discovery is complete.
static int on_svc(uint16_t conn, const struct ble_gatt_error *err, const struct ble_gatt_svc *svc, void *arg)
{
    (void)conn; (void)arg;
    if (svc) {
        char u[BLE_UUID_STR_LEN]; ble_uuid_to_str(&svc->uuid.u, u);
        report("  svc %u-%u %s", svc->start_handle, svc->end_handle, u);
        s_nsvc++;
        return 0;
    }
    xEventGroupSetBits(s_evt, BIT_DISC_DONE);   // err->status == BLE_HS_EDONE (or any terminal status)
    return 0;
}

static int gap_event(struct ble_gap_event *event, void *arg)
{
    (void)arg;
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        report("connect status=%d", event->connect.status);
        if (event->connect.status == 0) { s_conn = event->connect.conn_handle; xEventGroupSetBits(s_evt, BIT_CONNECTED); }
        else xEventGroupSetBits(s_evt, BIT_FAIL);
        return 0;
    case BLE_GAP_EVENT_DISCONNECT:
        report("disconnect reason=%d", event->disconnect.reason);
        xEventGroupSetBits(s_evt, BIT_DISCONNECT);
        return 0;
    case BLE_GAP_EVENT_CONN_UPDATE_REQ:
        return 0;   // accept the peer's request as-is (the spike doesn't page, so speed doesn't matter)
    default:
        return 0;
    }
}

static void probe_task(void *arg)
{
    (void)arg;
    EventBits_t bits = xEventGroupWaitBits(s_evt, BIT_CONNECTED | BIT_FAIL | BIT_DISCONNECT,
                                           pdTRUE, pdFALSE, pdMS_TO_TICKS(12000));
    if (!(bits & BIT_CONNECTED)) { report("probe %s: connect FAILED (central role unavailable?)", s_mac); goto done; }
    report("probe %s: CONNECTED — discovering services…", s_mac);
    vTaskDelay(pdMS_TO_TICKS(400));   // settle (avoids the ENOTCONN race the edge pull hit)
    s_nsvc = 0;
    ble_gattc_disc_all_svcs(s_conn, on_svc, NULL);
    bits = xEventGroupWaitBits(s_evt, BIT_DISC_DONE | BIT_DISCONNECT, pdTRUE, pdFALSE, pdMS_TO_TICKS(8000));
    if (bits & BIT_DISC_DONE)
        report("probe %s: OK — %d services (GATT CENTRAL over C6 HCI WORKS)", s_mac, s_nsvc);
    else
        report("probe %s: discovery timed out / disconnected", s_mac);
    ble_gap_terminate(s_conn, BLE_ERR_REM_USER_CONN_TERM);
    xEventGroupWaitBits(s_evt, BIT_DISCONNECT, pdTRUE, pdFALSE, pdMS_TO_TICKS(3000));
done:
    ha_ble_scan_resume();
    s_busy = false;
    vTaskDelete(NULL);
}

bool gatt_probe_start(const char *mac_str)
{
    if (s_busy) { report("probe busy"); return false; }
    if (!s_evt) s_evt = xEventGroupCreate();
    ble_addr_t addr;
    if (!ha_ble_lookup_addr(mac_str, &addr)) { report("probe %s: addr not cached (not heard advertising yet)", mac_str); return false; }
    snprintf(s_mac, sizeof(s_mac), "%s", mac_str);
    s_busy = true;
    xEventGroupClearBits(s_evt, 0xFF);
    ha_ble_scan_pause();                       // single radio: free it for the connection
    report("probe %s: connecting (addr_type=%d)…", mac_str, addr.type);
    int rc = ble_gap_connect(ha_ble_own_addr_type(), &addr, 10000, &CONN, gap_event, NULL);
    if (rc != 0) {
        report("probe %s: ble_gap_connect rc=%d", mac_str, rc);
        ha_ble_scan_resume(); s_busy = false; return false;
    }
    xTaskCreate(probe_task, "gattprobe", 4096, NULL, 5, NULL);
    return true;
}
