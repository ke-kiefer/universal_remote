// WLAN station. Several APs may share the SSID (mesh / multiple access points),
// so before every connect attempt we scan, log all candidates and pin the strongest BSSID.

#include "wifi.h"

#include <string.h>

#include "app_env.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"

static const char *TAG = "wifi";

#define BIT_CONNECTED BIT0
#define MAX_SCAN 16

static EventGroupHandle_t s_events;
static esp_netif_t *s_netif;
static int64_t s_start_us;
static wifi_config_t s_cfg;
static esp_timer_handle_t s_retry_timer;
static volatile bool s_suspended;

static void start_scan(void)
{
    wifi_scan_config_t sc = {
        .ssid = (uint8_t *)APP_WIFI_SSID,
        .show_hidden = false,
        .scan_type = WIFI_SCAN_TYPE_ACTIVE,
    };
    esp_err_t err = esp_wifi_scan_start(&sc, false);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "scan start failed (%s), connecting without BSSID", esp_err_to_name(err));
        s_cfg.sta.bssid_set = false;
        esp_wifi_set_config(WIFI_IF_STA, &s_cfg);
        esp_wifi_connect();
    }
}

static void on_scan_done(void)
{
    // Static: runs in the system event task, whose stack is small (~2.3 KB).
    static wifi_ap_record_t recs[MAX_SCAN];
    uint16_t n = MAX_SCAN;
    if (esp_wifi_scan_get_ap_records(&n, recs) != ESP_OK) n = 0;

    int best = -1;
    for (int i = 0; i < n; i++) {
        ESP_LOGI(TAG, "  AP " MACSTR "  ch %2d  %4d dBm", MAC2STR(recs[i].bssid),
                 recs[i].primary, recs[i].rssi);
        if (best < 0 || recs[i].rssi > recs[best].rssi) best = i;
    }
    if (best >= 0) {
        ESP_LOGI(TAG, "scan: %d AP(s) for '%s', using " MACSTR " (ch %d, %d dBm)", n,
                 APP_WIFI_SSID, MAC2STR(recs[best].bssid), recs[best].primary, recs[best].rssi);
        memcpy(s_cfg.sta.bssid, recs[best].bssid, 6);
        s_cfg.sta.bssid_set = true;
        s_cfg.sta.channel = recs[best].primary;
    } else {
        ESP_LOGW(TAG, "scan: no AP found for '%s'", APP_WIFI_SSID);
        s_cfg.sta.bssid_set = false;
        s_cfg.sta.channel = 0;
    }
    esp_wifi_set_config(WIFI_IF_STA, &s_cfg);
    esp_wifi_connect();
}

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        start_scan();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_SCAN_DONE) {
        on_scan_done();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *d = data;
        xEventGroupClearBits(s_events, BIT_CONNECTED);
        if (s_suspended) return;  // wifi_suspend(): no reconnect
        ESP_LOGW(TAG, "disconnected (reason %d, rssi %d), rescanning", d->reason, d->rssi);
        // Do not block the event task; rescan after a short pause.
        esp_timer_start_once(s_retry_timer, 500 * 1000);
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_CONNECTED) {
        const wifi_event_sta_connected_t *c = data;
        ESP_LOGI(TAG, "associated with " MACSTR " ch %d", MAC2STR(c->bssid), c->channel);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *e = data;
        ESP_LOGI(TAG, "connected, ip=" IPSTR ", rssi %d dBm, after %lld ms",
                 IP2STR(&e->ip_info.ip), wifi_get_rssi(),
                 (esp_timer_get_time() - s_start_us) / 1000);
        xEventGroupSetBits(s_events, BIT_CONNECTED);
    }
}

static void apply_radio_settings(void)
{
    // No modem sleep while active: with a weak signal power save causes missed beacons and
    // disconnects, and it adds latency (modem sleep only while idle, see power.c).
    esp_wifi_set_ps(WIFI_PS_NONE);
    if (APP_WIFI_TX_POWER_DBM > 0) {
        // Unit: 0.25 dBm. Lower TX power can help boards with a poorly matched antenna.
        esp_wifi_set_max_tx_power((int8_t)(APP_WIFI_TX_POWER_DBM * 4));
    }
}

esp_err_t wifi_start(void)
{
    if (strlen(APP_WIFI_SSID) == 0) {
        ESP_LOGW(TAG, "WIFI_SSID empty, WLAN disabled");
        return ESP_ERR_INVALID_STATE;
    }
    s_events = xEventGroupCreate();
    const esp_timer_create_args_t targs = {
        .callback = (esp_timer_cb_t)start_scan,
        .name = "wifi_retry",
    };
    ESP_ERROR_CHECK(esp_timer_create(&targs, &s_retry_timer));
    ESP_ERROR_CHECK(esp_netif_init());
    esp_err_t err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) return err;
    s_netif = esp_netif_create_default_wifi_sta();
    esp_netif_set_hostname(s_netif, "universal-" APP_DEVICE_NAME);

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));

    memset(&s_cfg, 0, sizeof(s_cfg));
    strlcpy((char *)s_cfg.sta.ssid, APP_WIFI_SSID, sizeof(s_cfg.sta.ssid));
    strlcpy((char *)s_cfg.sta.password, APP_WIFI_PASSWORD, sizeof(s_cfg.sta.password));
    s_cfg.sta.threshold.authmode =
        strlen(APP_WIFI_PASSWORD) ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    s_cfg.sta.pmf_cfg.capable = true;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &s_cfg));
    s_start_us = esp_timer_get_time();
    ESP_ERROR_CHECK(esp_wifi_start());
    apply_radio_settings();
    int8_t txp = 0;
    esp_wifi_get_max_tx_power(&txp);
    ESP_LOGI(TAG, "max TX power %.2f dBm", txp / 4.0);
    ESP_LOGI(TAG, "scanning for '%s'", APP_WIFI_SSID);
    return ESP_OK;
}

bool wifi_wait_connected(int timeout_ms)
{
    if (!s_events) return false;
    EventBits_t bits = xEventGroupWaitBits(s_events, BIT_CONNECTED, pdFALSE, pdTRUE,
                                           pdMS_TO_TICKS(timeout_ms));
    return bits & BIT_CONNECTED;
}

bool wifi_is_connected(void)
{
    return s_events && (xEventGroupGetBits(s_events) & BIT_CONNECTED);
}

void wifi_get_ip(char *buf, size_t len)
{
    esp_netif_ip_info_t ip;
    if (wifi_is_connected() && esp_netif_get_ip_info(s_netif, &ip) == ESP_OK) {
        snprintf(buf, len, IPSTR, IP2STR(&ip.ip));
    } else {
        strlcpy(buf, "-", len);
    }
}

int wifi_get_rssi(void)
{
    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) return ap.rssi;
    return 0;
}

void wifi_set_power_save(bool on)
{
    esp_err_t err = esp_wifi_set_ps(on ? WIFI_PS_MIN_MODEM : WIFI_PS_NONE);
    ESP_LOGI(TAG, "modem sleep %s (%s)", on ? "on" : "off", esp_err_to_name(err));
}

void wifi_suspend(void)
{
    if (!s_events || s_suspended) return;
    s_suspended = true;
    esp_timer_stop(s_retry_timer);
    esp_wifi_disconnect();
    esp_wifi_stop();
    xEventGroupClearBits(s_events, BIT_CONNECTED);
    ESP_LOGI(TAG, "stopped");
}

void wifi_resume(void)
{
    if (!s_events || !s_suspended) return;
    s_suspended = false;
    s_start_us = esp_timer_get_time();
    esp_wifi_start();  // STA_START -> scan -> connect
    apply_radio_settings();
    ESP_LOGI(TAG, "restarted, scanning");
}
