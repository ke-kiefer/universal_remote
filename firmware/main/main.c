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
#include "power.h"
#include <string.h>
#include "ui.h"
#include "wifi.h"

static const char *TAG = "main";

#define STATUS_INTERVAL_US (10 * 1000000LL)
#define STATUS_INTERVAL_IDLE_US (60 * 1000000LL)  // fewer requests while idle

typedef struct {
    ui_event_t type;
    int arg;
} app_event_t;

static QueueHandle_t s_events;
static bool s_camera_ok;
static bool s_battery_ok;
static int s_sent;

// Last identify result: needed for actions, the choose screen and feedback.
static api_identify_t s_ident;
static api_entity_view_t s_view;
static bool s_feedback_sent;  // at most one feedback per identify

static void on_ui_event(ui_event_t ev, int arg)
{
    // Called in the LVGL task: never block here, just hand over to the app task.
    app_event_t e = {.type = ev, .arg = arg};
    xQueueSend(s_events, &e, 0);
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

// Focus (if needed), capture, check JPEG. Returns NULL on failure (toast already shown).
static camera_fb_t *focus_and_capture(camera_af_result_t *af, int *cap_ms)
{
    camera_af_start();
    *af = camera_af_wait(APP_AF_TIMEOUT_MS);
    int64_t t0 = esp_timer_get_time();
    camera_fb_t *fb = camera_capture();
    *cap_ms = (int)((esp_timer_get_time() - t0) / 1000);
    return fb;
}

static void identify(void)
{
    if (!s_camera_ok) {
        ui_toast("Kamera nicht verfügbar", true);
        return;
    }
    if (!wifi_is_connected()) {
        ui_toast("Kein WLAN – bitte warten", true);
        return;
    }
    int64_t t_start = esp_timer_get_time();
    ui_show_busy("Fokussiere …");
    camera_af_result_t af;
    int cap_ms;
    camera_fb_t *fb = focus_and_capture(&af, &cap_ms);
    if (!fb) {
        ui_show_home();
        ui_toast("Aufnahme fehlgeschlagen", true);
        return;
    }
    ui_show_busy("Erkenne …");
    char meta[96];
    snprintf(meta, sizeof(meta), "af=%s&af_ms=%d&af_raw=%02x&cap_ms=%d&rssi=%d",
             !af.available ? "na" : af.focused ? "ok" : "timeout", af.ms, af.raw, cap_ms,
             wifi_get_rssi());
    int64_t t_up = esp_timer_get_time();
    size_t len = fb->len;
    s_feedback_sent = false;
    esp_err_t err = api_identify(fb->buf, fb->len, meta, &s_ident);
    camera_release(fb);
    int64_t t_end = esp_timer_get_time();

    ESP_LOGI(TAG,
             "TIMING identify af=%d ms capture=%d ms request=%lld ms (server %d ms) total=%lld "
             "ms size=%u status=%d",
             af.ms, cap_ms, (t_end - t_up) / 1000, s_ident.server_ms, (t_end - t_start) / 1000,
             (unsigned)len, err == ESP_OK ? (int)s_ident.status : -1);
    if (err != ESP_OK) {
        ui_show_home();
        ui_toast("Server nicht erreichbar (WLAN schwach?)", true);
        return;
    }
    switch (s_ident.status) {
    case API_MATCH:
        s_view = s_ident.best;
        ui_show_result(&s_view);
        break;
    case API_UNCERTAIN:
        ui_show_choose(s_ident.candidates, s_ident.n_candidates);
        break;
    case API_UNKNOWN:
        ui_show_home();
        ui_toast(s_ident.n_candidates ? "Nicht erkannt" : "Nicht erkannt – noch keine Trainingsdaten?",
                 true);
        break;
    }
}

static void run_action(int idx)
{
    if (idx < 0 || idx >= s_view.n_actions) return;
    const api_action_t *a = &s_view.actions[idx];
    ESP_LOGI(TAG, "action %s on %s", a->id, s_view.entity.entity_id);
    ui_result_busy(true);
    api_entity_view_t upd = s_view;
    esp_err_t err = api_action(s_view.entity.entity_id, a->id, &upd);
    if (err == ESP_OK) {
        // Keep name/area/confidence, take the new state and actions.
        memcpy(s_view.state_text, upd.state_text, sizeof(s_view.state_text));
        s_view.n_actions = upd.n_actions;
        memcpy(s_view.actions, upd.actions, sizeof(s_view.actions));
        ui_update_result(&s_view);
        ui_result_busy(false);
        // Using an action on a match confirms the recognition implicitly: send the capture
        // as training image once (the server skips near-duplicates). Leaving with "Zurück"
        // without an action sends nothing, since that hints at a wrong recognition.
        if (!s_feedback_sent && s_ident.capture_id[0]) {
            s_feedback_sent = true;
            api_feedback(s_ident.capture_id, s_view.entity.entity_id, "action");
        }
        return;
    }
    ui_toast("Aktion fehlgeschlagen", true);
    ui_result_busy(false);
}

static void choose(int idx)
{
    if (idx < 0) {
        // "Keins davon": keep the capture in the inbox for manual labeling.
        ui_show_home();
        if (api_feedback(s_ident.capture_id, NULL, "none") == ESP_OK) {
            ui_toast("Bild zum Zuordnen gespeichert", false);
        }
        return;
    }
    if (idx >= s_ident.n_candidates) return;
    const api_entity_t *c = &s_ident.candidates[idx];
    ui_show_busy("Lade …");
    // Feedback first: the confirmed capture becomes a training image (improves the index).
    s_feedback_sent = true;
    if (api_feedback(s_ident.capture_id, c->entity_id, "choose") != ESP_OK) {
        ESP_LOGW(TAG, "feedback for %s failed", c->entity_id);
    }
    if (api_entity(c->entity_id, &s_view) != ESP_OK) {
        ui_show_home();
        ui_toast("Gerät konnte nicht geladen werden", true);
        return;
    }
    s_view.entity.confidence = c->confidence;
    ui_show_result(&s_view);
}

static void app_task(void *arg)
{
    if (wifi_start() != ESP_OK) ui_toast("WLAN deaktiviert (.env)", true);
    int64_t next_status = 0;
    bool was_connected = false;
    while (true) {
        app_event_t ev;
        if (xQueueReceive(s_events, &ev, pdMS_TO_TICKS(500)) == pdTRUE) {
            switch (ev.type) {
            case UI_EV_TRAIN_PHOTO:
                train_photo();
                break;
            case UI_EV_IDENTIFY:
                identify();
                break;
            case UI_EV_ACTION:
                run_action(ev.arg);
                break;
            case UI_EV_CHOOSE:
                choose(ev.arg);
                break;
            case UI_EV_WAKE:
                power_wake();
                break;
            }
            // Requests can take seconds: count inactivity from when they are done.
            power_kick();
        }
        power_update();
        // Refresh the status bar periodically and immediately on WLAN state changes.
        bool connected = wifi_is_connected();
        if (connected != was_connected || esp_timer_get_time() >= next_status) {
            was_connected = connected;
            next_status = esp_timer_get_time() + (power_state() == POWER_IDLE
                                                      ? STATUS_INTERVAL_IDLE_US
                                                      : STATUS_INTERVAL_US);
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

    s_events = xQueueCreate(4, sizeof(app_event_t));
    ui_init(on_ui_event);

    s_battery_ok = battery_init() == ESP_OK;
    s_camera_ok = camera_init() == ESP_OK;
    if (!s_camera_ok) ui_toast("Kamera-Init fehlgeschlagen", true);

    power_init();
    xTaskCreate(app_task, "app", 8192, NULL, 4, NULL);
    ESP_LOGI(TAG, "ready");
}
