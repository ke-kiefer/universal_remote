// Universal Remote firmware – bring-up: display, touch, WLAN, server health, camera capture.
// The real UI state machine (HOME/RESULT/TRAIN ...) replaces the test screen later.

#include "api_client.h"
#include "app_env.h"
#include "board.h"
#include "camera.h"
#include "esp_log.h"
#include "esp_lvgl_port.h"
#include "esp_psram.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "wifi.h"

static const char *TAG = "main";

typedef enum { CMD_CAPTURE } cmd_t;

static QueueHandle_t s_cmds;
static bool s_camera_ok;
static lv_obj_t *s_status;
static lv_obj_t *s_result;
static lv_obj_t *s_coords;
static lv_obj_t *s_btn;
static int s_sent;

static void ui_set_text(lv_obj_t *label, const char *text)
{
    if (lvgl_port_lock(0)) {
        lv_label_set_text(label, text);
        lvgl_port_unlock();
    }
}

static void ui_set_busy(bool busy)
{
    if (lvgl_port_lock(0)) {
        if (busy) lv_obj_add_state(s_btn, LV_STATE_DISABLED);
        else lv_obj_remove_state(s_btn, LV_STATE_DISABLED);
        lvgl_port_unlock();
    }
}

static void do_capture(void)
{
    char text[160], id[48];
    if (!s_camera_ok) {
        ui_set_text(s_result, "Camera not available");
        return;
    }
    if (!wifi_is_connected()) {
        ui_set_text(s_result, "No WLAN");
        return;
    }
    ui_set_busy(true);
    ui_set_text(s_result, "Focusing...");
    camera_af_start();
    camera_af_result_t af = camera_af_wait(APP_AF_TIMEOUT_MS);
    ui_set_text(s_result, "Capturing...");
    int64_t t0 = esp_timer_get_time();
    camera_fb_t *fb = camera_capture();
    int64_t t1 = esp_timer_get_time();
    if (!fb) {
        ui_set_text(s_result, "Capture failed");
        ui_set_busy(false);
        return;
    }
    size_t len = fb->len;
    unsigned w = fb->width, h = fb->height;
    ui_set_text(s_result, "Uploading...");
    char meta[80];
    snprintf(meta, sizeof(meta), "af=%s&af_ms=%d&af_raw=%02x&cap_ms=%d&rssi=%d",
             !af.available ? "na" : af.focused ? "ok" : "timeout", af.ms, af.raw,
             (int)((t1 - t0) / 1000), wifi_get_rssi());
    esp_err_t err = api_capture(fb->buf, fb->len, meta, id, sizeof(id));
    int64_t t2 = esp_timer_get_time();
    camera_release(fb);

    int cap_ms = (int)((t1 - t0) / 1000), up_ms = (int)((t2 - t1) / 1000);
    const char *af_txt = !af.available ? "n/a" : af.focused ? "ok" : "TIMEOUT";
    ESP_LOGI(TAG,
             "TIMING af=%d ms (%s, 0x%02x) capture=%d ms upload=%d ms size=%u bytes %ux%u "
             "result=%s",
             af.ms, af_txt, af.raw, cap_ms, up_ms, (unsigned)len, w, h,
             err == ESP_OK ? "ok" : "fail");
    if (err == ESP_OK) {
        s_sent++;
        snprintf(text, sizeof(text),
                 "Sent #%d (%ux%u, %u KB)\nAF %s %d ms, capture %d ms\nupload %d ms", s_sent, w,
                 h, (unsigned)(len / 1024), af_txt, af.ms, cap_ms, up_ms);
    } else {
        snprintf(text, sizeof(text), "Upload failed\n(%u KB, %d ms)", (unsigned)(len / 1024),
                 up_ms);
    }
    ui_set_text(s_result, text);
    ui_set_busy(false);
}

// Network task: all HTTP work happens here, never in the LVGL task.
static void net_task(void *arg)
{
    if (wifi_start() != ESP_OK) {
        ui_set_text(s_status, "WLAN: disabled");
    }
    char line[96], ip[16];
    int64_t next_health = 0;
    while (true) {
        cmd_t cmd;
        if (xQueueReceive(s_cmds, &cmd, pdMS_TO_TICKS(1000)) == pdTRUE && cmd == CMD_CAPTURE) {
            do_capture();
        }
        if (esp_timer_get_time() < next_health) continue;
        next_health = esp_timer_get_time() + 10 * 1000000LL;

        bool up = wifi_is_connected();
        api_health_t h = {0};
        esp_err_t herr = up ? api_health(&h) : ESP_FAIL;
        wifi_get_ip(ip, sizeof(ip));
        if (!up) {
            snprintf(line, sizeof(line), "WLAN: connecting...\nServer: -");
        } else if (herr == ESP_OK) {
            snprintf(line, sizeof(line), "WLAN: %s (%d dBm)\nServer: ok, %d ent / %d img", ip,
                     wifi_get_rssi(), h.entities, h.images);
        } else if (herr == ESP_ERR_INVALID_ARG) {
            snprintf(line, sizeof(line), "WLAN: %s (%d dBm)\nServer: wrong API key", ip,
                     wifi_get_rssi());
        } else {
            snprintf(line, sizeof(line), "WLAN: %s (%d dBm)\nServer: offline", ip,
                     wifi_get_rssi());
        }
        ui_set_text(s_status, line);
    }
}

static void show_point(const char *what, lv_point_t p)
{
    ESP_LOGI(TAG, "%s at x=%d y=%d", what, (int)p.x, (int)p.y);
    lv_label_set_text_fmt(s_coords, "Last touch: x=%d y=%d", (int)p.x, (int)p.y);
}

static void btn_event_cb(lv_event_t *e)
{
    lv_point_t p;
    switch (lv_event_get_code(e)) {
    case LV_EVENT_PRESSED:
        lv_indev_get_point(lv_indev_active(), &p);
        show_point("button pressed", p);
        break;
    case LV_EVENT_CLICKED: {
        cmd_t cmd = CMD_CAPTURE;
        xQueueSend(s_cmds, &cmd, 0);
        break;
    }
    default:
        break;
    }
}

static void screen_touch_cb(lv_event_t *e)
{
    lv_point_t p;
    lv_indev_get_point(lv_indev_active(), &p);
    show_point("screen touch", p);
}

static lv_obj_t *make_label(lv_obj_t *parent, const lv_font_t *font, uint32_t color)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(color), 0);
    return l;
}

static void build_test_ui(void)
{
    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(0x101418), 0);
    lv_obj_add_event_cb(scr, screen_touch_cb, LV_EVENT_PRESSED, NULL);

    s_status = make_label(scr, &lv_font_montserrat_14, 0x9AD0FF);
    lv_label_set_text(s_status, "WLAN: starting...\nServer: -");
    lv_obj_align(s_status, LV_ALIGN_TOP_LEFT, 6, 4);

    s_result = make_label(scr, &lv_font_montserrat_14, 0xFFFFFF);
    lv_label_set_text(s_result, "Training test:\npress 'Foto' to send an image");
    lv_obj_set_width(s_result, 228);
    lv_label_set_long_mode(s_result, LV_LABEL_LONG_WRAP);
    lv_obj_align(s_result, LV_ALIGN_TOP_LEFT, 6, 60);

    s_coords = make_label(scr, &lv_font_montserrat_14, 0xFFD000);
    lv_label_set_text(s_coords, "Last touch: -");
    lv_obj_align(s_coords, LV_ALIGN_TOP_LEFT, 6, 150);

    s_btn = lv_button_create(scr);
    lv_obj_set_size(s_btn, 200, 90);
    lv_obj_align(s_btn, LV_ALIGN_BOTTOM_MID, 0, -24);
    lv_obj_add_event_cb(s_btn, btn_event_cb, LV_EVENT_ALL, NULL);
    lv_obj_t *bl = lv_label_create(s_btn);
    lv_label_set_text(bl, "Foto");
    lv_obj_set_style_text_font(bl, &lv_font_montserrat_28, 0);
    lv_obj_center(bl);
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

    if (lvgl_port_lock(0)) {
        build_test_ui();
        lvgl_port_unlock();
    }

    s_camera_ok = camera_init() == ESP_OK;
    if (!s_camera_ok) ui_set_text(s_result, "Camera init FAILED\n(see log)");

    s_cmds = xQueueCreate(4, sizeof(cmd_t));
    xTaskCreate(net_task, "net", 6144, NULL, 4, NULL);
    ESP_LOGI(TAG, "ready");
}
