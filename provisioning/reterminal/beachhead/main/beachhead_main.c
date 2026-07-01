// D1001 beachhead — prove the connectivity stack on the ESP32-P4:
//   NVS -> netif/event -> esp_wifi (routed to the C6 over esp-hosted/SDIO)
//   -> join CTWap_24g -> get IP -> MQTT to 192.168.0.210 -> publish "hello".
// No display: the point of the beachhead is to prove the network path (the C6
// link is the real unknown) BEFORE any UI. Watch `idf.py monitor` for proof of life.
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_wifi.h"          // esp_wifi_remote provides these symbols; radio lives on the C6
#include "esp_event.h"
#include "esp_netif.h"
#include "nvs_flash.h"
#include "mqtt_client.h"
#include "secrets.h"

static const char *TAG = "beachhead";
static EventGroupHandle_t s_evt;
#define WIFI_CONNECTED_BIT BIT0

static void mqtt_event_handler(void *args, esp_event_base_t base, int32_t id, void *data)
{
    esp_mqtt_event_handle_t e = (esp_mqtt_event_handle_t)data;
    switch ((esp_mqtt_event_id_t)id) {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "MQTT CONNECTED to %s", MQTT_BROKER_URI);
        esp_mqtt_client_publish(e->client, "d1001-beachhead/status",
            "{\"device\":\"d1001-beachhead\",\"status\":\"online\","
            "\"msg\":\"hello from ESP32-P4 via C6\"}", 0, 1, 1);
        ESP_LOGI(TAG, ">>> BEACHHEAD SUCCESS: published hello to d1001-beachhead/status <<<");
        break;
    case MQTT_EVENT_DISCONNECTED: ESP_LOGW(TAG, "MQTT disconnected"); break;
    case MQTT_EVENT_ERROR:        ESP_LOGE(TAG, "MQTT error"); break;
    default: break;
    }
}

static void start_mqtt(void)
{
    esp_mqtt_client_config_t cfg = { .broker.address.uri = MQTT_BROKER_URI };
    esp_mqtt_client_handle_t c = esp_mqtt_client_init(&cfg);
    esp_mqtt_client_register_event(c, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    esp_mqtt_client_start(c);
}

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "WiFi disconnected — reconnecting");
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *evt = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "GOT IP: " IPSTR, IP2STR(&evt->ip_info.ip));
        xEventGroupSetBits(s_evt, WIFI_CONNECTED_BIT);
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "=== D1001 beachhead: ESP32-P4 + ESP32-C6 (esp-hosted/SDIO) ===");

    esp_err_t r = nvs_flash_init();
    if (r == ESP_ERR_NVS_NO_FREE_PAGES || r == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    s_evt = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));   // -> esp_wifi_remote -> C6 radio over esp-hosted

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                        wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                        wifi_event_handler, NULL, NULL));

    wifi_config_t wc = { 0 };
    strncpy((char *)wc.sta.ssid, WIFI_SSID, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, WIFI_PASS, sizeof(wc.sta.password) - 1);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "WiFi started — joining %s", WIFI_SSID);

    xEventGroupWaitBits(s_evt, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);
    ESP_LOGI(TAG, "WiFi up — starting MQTT");
    start_mqtt();
}
