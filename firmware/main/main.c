// Universal Remote firmware.
//
// Tasks:
//   LVGL task (esp_lvgl_port): renders the UI, button callbacks only post events.
//   app task (this file):      WLAN/server status, camera, all HTTP requests.

#include "api_client.h"
#include "app_env.h"
#include "battery.h"
#include "board.h"
#include "camera.h"
#include "esp_log.h"
#include "esp_psram.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "lvgl.h"
#include "nvs_flash.h"
#include "ui.h"
#include "wifi.h"

static const char *TAG = "main";

#define STATUS_INTERVAL_US (10 * 1000000LL)

static QueueHandle_t s_events;
static bool s_camera_ok;
static bool s_battery_ok;
static int s_sent;

static void on_ui_event(ui_event_t ev)
{
    // Called in the LVGL task: never block here, just hand over to the app task.
    xQueueSend(s_events, &ev, 0);
}

static void update_status(void)
{
    ui_status_t st = {.wifi = wifi_is_connected(), .battery_v = -1.0f};
    if (st.wifi) {
        st.rssi = wifi_get_rssi();
        api_health_t h = {0};
        esp_err_t err = api_health(&h);
        st.server = err == ESP_OK                ? UI_SRV_OK
                    : err == ESP_ERR_INVALID_ARG ? UI_SRV_AUTH
                                                 : UI_SRV_OFFLINE;
        st.entities = h.entities;
        st.images = h.images;
    }
    if (s_battery_ok) {
        st.battery_v = battery_voltage();
        st.battery_pct = battery_percent(st.battery_v);
    }
    ui_set_status(&st);
}

static void train_photo(void)
{
    char detail[128], id[48], meta[96];
    if (!s_camera_ok) {
        ui_toast("Kamera nicht verfügbar", true);
        return;
    }
    if (!wifi_is_connected()) {
        ui_toast("Kein WLAN – bitte warten", true);
        return;
    }

    ui_train_busy("Fokussiere …");
    camera_af_start();
    camera_af_result_t af = camera_af_wait(APP_AF_TIMEOUT_MS);

    int64_t t0 = esp_timer_get_time();
    camera_fb_t *fb = camera_capture();
    int64_t t1 = esp_timer_get_time();
    if (!fb) {
        ui_train_done(false, s_sent, "Aufnahme fehlgeschlagen");
        return;
    }
    ui_train_busy("Sende …");
    int rssi = wifi_get_rssi();
    snprintf(meta, sizeof(meta), "af=%s&af_ms=%d&af_raw=%02x&cap_ms=%d&rssi=%d",
             !af.available ? "na" : af.focused ? "ok" : "timeout", af.ms, af.raw,
             (int)((t1 - t0) / 1000), rssi);
    size_t len = fb->len;
    esp_err_t err = api_capture(fb->buf, fb->len, meta, id, sizeof(id));
    int64_t t2 = esp_timer_get_time();
    camera_release(fb);

    int cap_ms = (int)((t1 - t0) / 1000), up_ms = (int)((t2 - t1) / 1000);
    ESP_LOGI(TAG, "TIMING af=%d ms (%s) capture=%d ms upload=%d ms size=%u rssi=%d result=%s",
             af.ms, af.focused ? "focused" : "not focused", cap_ms, up_ms, (unsigned)len, rssi,
             err == ESP_OK ? "ok" : "fail");
    if (err == ESP_OK) {
        s_sent++;
        snprintf(detail, sizeof(detail), LV_SYMBOL_OK " %u KB · Fokus %s · %.1f s",
                 (unsigned)(len / 1024), af.focused ? "ok" : "unscharf?",
                 (af.ms + cap_ms + up_ms) / 1000.0);
        ui_train_done(true, s_sent, detail);
    } else {
        ui_train_done(false, s_sent, "Senden fehlgeschlagen – nochmal versuchen");
        ui_toast("Senden fehlgeschlagen (WLAN schwach?)", true);
    }
}

static void app_task(void *arg)
{
    if (wifi_start() != ESP_OK) ui_toast("WLAN deaktiviert (.env)", true);
    int64_t next_status = 0;
    bool was_connected = false;
    while (true) {
        ui_event_t ev;
        if (xQueueReceive(s_events, &ev, pdMS_TO_TICKS(500)) == pdTRUE) {
            switch (ev) {
            case UI_EV_TRAIN_PHOTO:
                train_photo();
                break;
            case UI_EV_IDENTIFY:
                ui_toast("Erkennung kommt im nächsten Schritt", false);
                break;
            }
        }
        // Refresh the status bar periodically and immediately on WLAN state changes.
        bool connected = wifi_is_connected();
        if (connected != was_connected || esp_timer_get_time() >= next_status) {
            was_connected = connected;
            next_status = esp_timer_get_time() + STATUS_INTERVAL_US;
            update_status();
        }
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "Universal Remote starting, device=%s server=%s", APP_DEVICE_NAME,
             APP_SERVER_URL);
    ESP_LOGI(TAG, "PSRAM: %u KB", (unsigned)(esp_psram_get_size() / 1024));

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    lv_display_t *disp = NULL;
    ESP_ERROR_CHECK(board_display_init(&disp));
    board_backlight_set(APP_BACKLIGHT_PCT);

    s_events = xQueueCreate(4, sizeof(ui_event_t));
    ui_init(on_ui_event);

    s_battery_ok = battery_init() == ESP_OK;
    s_camera_ok = camera_init() == ESP_OK;
    if (!s_camera_ok) ui_toast("Kamera-Init fehlgeschlagen", true);

    xTaskCreate(app_task, "app", 8192, NULL, 4, NULL);
    ESP_LOGI(TAG, "ready");
}
