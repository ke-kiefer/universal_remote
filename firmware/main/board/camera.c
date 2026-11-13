#include "camera.h"

#include "app_env.h"
#include "board_pins.h"
#include "esp_camera_af.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "camera";

#define WARMUP_FRAMES 3
#define AF_LOAD_TIMEOUT_MS 3000

// OV5640 AF firmware command interface. Same registers and sequence as esp32-camera's
// sensors/ov5640_af.c and the Arduino library ESP32_OV5640_AF (OmniVision AF firmware).
#define AF_CMD_MAIN 0x3022
#define AF_CMD_ACK 0x3023
#define AF_FW_STATUS 0x3029
#define AF_MAIN_RELEASE 0x08     // release focus, back to idle
#define AF_MAIN_SINGLE 0x03      // trigger single AF
#define AF_MAIN_CONTINUOUS 0x04  // continuous AF
#define AF_STATUS_FOCUSED 0x10
#define AF_STATUS_IDLE 0x70

static bool s_af_ok;
static int64_t s_af_start_us;

static bool jpeg_complete(const camera_fb_t *fb)
{
    if (fb->len < 4 || fb->buf[0] != 0xFF || fb->buf[1] != 0xD8) return false;
    // The EOI marker may be followed by a few padding bytes.
    size_t start = fb->len > 64 ? fb->len - 64 : 0;
    for (size_t i = fb->len - 1; i > start; i--) {
        if (fb->buf[i - 1] == 0xFF && fb->buf[i] == 0xD9) return true;
    }
    return false;
}

static int af_read(uint16_t reg)
{
    sensor_t *s = esp_camera_sensor_get();
    return s->get_reg(s, reg, 0xff);
}

// Send an AF command: ACK=1, MAIN=cmd. The AF firmware clears ACK when the command is done.
// Does not wait (single AF only clears ACK after focusing, which can take seconds).
static void af_command(uint8_t cmd)
{
    sensor_t *s = esp_camera_sensor_get();
    s->set_reg(s, AF_CMD_ACK, 0xff, 0x01);
    s->set_reg(s, AF_CMD_MAIN, 0xff, cmd);
}

static void af_wait_ack(int timeout_ms)
{
    int64_t end = esp_timer_get_time() + (int64_t)timeout_ms * 1000;
    while (af_read(AF_CMD_ACK) != 0x00 && esp_timer_get_time() < end) {
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

esp_err_t camera_init(void)
{
    const camera_config_t cfg = {
        .pin_pwdn = BOARD_CAM_PIN_PWDN,
        .pin_reset = BOARD_CAM_PIN_RESET,
        .pin_xclk = BOARD_CAM_PIN_XCLK,
        .pin_sccb_sda = BOARD_CAM_PIN_SIOD,
        .pin_sccb_scl = BOARD_CAM_PIN_SIOC,
        .pin_d7 = BOARD_CAM_PIN_D7,
        .pin_d6 = BOARD_CAM_PIN_D6,
        .pin_d5 = BOARD_CAM_PIN_D5,
        .pin_d4 = BOARD_CAM_PIN_D4,
        .pin_d3 = BOARD_CAM_PIN_D3,
        .pin_d2 = BOARD_CAM_PIN_D2,
        .pin_d1 = BOARD_CAM_PIN_D1,
        .pin_d0 = BOARD_CAM_PIN_D0,
        .pin_vsync = BOARD_CAM_PIN_VSYNC,
        .pin_href = BOARD_CAM_PIN_HREF,
        .pin_pclk = BOARD_CAM_PIN_PCLK,
        .xclk_freq_hz = APP_CAMERA_XCLK_MHZ * 1000000,
        .ledc_timer = BOARD_CAM_LEDC_TIMER,
        .ledc_channel = BOARD_CAM_LEDC_CHANNEL,
        .pixel_format = PIXFORMAT_JPEG,
        .frame_size = APP_CAMERA_FRAMESIZE,
        .jpeg_quality = APP_CAMERA_JPEG_QUALITY,
        .fb_count = 1,
        .fb_location = CAMERA_FB_IN_PSRAM,
        .grab_mode = CAMERA_GRAB_LATEST,
        .sccb_i2c_port = BOARD_CAM_SCCB_PORT,
    };
    int64_t t0 = esp_timer_get_time();
    esp_err_t err = esp_camera_init(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "init failed: %s", esp_err_to_name(err));
        return err;
    }
    sensor_t *s = esp_camera_sensor_get();
    camera_sensor_info_t *info = esp_camera_sensor_get_info(&s->id);
    ESP_LOGI(TAG, "sensor %s (PID 0x%04x), framesize %s, quality %d, xclk %d MHz",
             info ? info->name : "?", s->id.PID, APP_CAMERA_FRAMESIZE_NAME,
             APP_CAMERA_JPEG_QUALITY, APP_CAMERA_XCLK_MHZ);
    s->set_hmirror(s, APP_CAMERA_HMIRROR);
    s->set_vflip(s, APP_CAMERA_VFLIP);

#ifndef APP_AF_MODE_OFF
    // Autofocus: esp32-camera ships the OV5640 AF firmware (same blob and register sequence
    // as the Arduino library ESP32_OV5640_AF by Eric Nam, MIT; originally OmniVision's
    // reference AF firmware). Loading writes ~4 KB over SCCB to 0x8000, then waits for
    // FW_STATUS (0x3029) == 0x70 (idle). Triggering is done here directly (non-blocking).
    if (esp_camera_af_is_supported(s)) {
        int64_t ta = esp_timer_get_time();
        const esp_camera_af_config_t af_cfg = {
            .mode = ESP_CAMERA_AF_MODE_MANUAL,
            .timeout_ms = AF_LOAD_TIMEOUT_MS,
        };
        s_af_ok = esp_camera_af_init(s, &af_cfg) == ESP_OK;
        ESP_LOGI(TAG, "AF firmware %s in %lld ms (status 0x%02x), mode %s",
                 s_af_ok ? "loaded" : "FAILED", (esp_timer_get_time() - ta) / 1000,
                 af_read(AF_FW_STATUS), APP_AF_MODE_NAME);
#ifdef APP_AF_MODE_CONTINUOUS
        if (s_af_ok) af_command(AF_MAIN_CONTINUOUS);
#endif
    } else {
        ESP_LOGW(TAG, "AF not supported (CONFIG_CAMERA_AF_SUPPORT off?)");
    }
#endif

    // Let exposure and white balance settle; measure the frame rate (drives AF speed).
    int64_t tf = esp_timer_get_time();
    for (int i = 0; i < WARMUP_FRAMES; i++) {
        camera_fb_t *fb = esp_camera_fb_get();
        if (fb) esp_camera_fb_return(fb);
    }
    int frame_ms = (int)((esp_timer_get_time() - tf) / 1000 / WARMUP_FRAMES);
    ESP_LOGI(TAG, "ready after %lld ms, ~%d ms per frame (~%d fps)",
             (esp_timer_get_time() - t0) / 1000, frame_ms, frame_ms ? 1000 / frame_ms : 0);
    return ESP_OK;
}

esp_err_t camera_af_start(void)
{
    if (!s_af_ok) return ESP_ERR_NOT_SUPPORTED;
    s_af_start_us = esp_timer_get_time();
#ifdef APP_AF_MODE_SINGLE
    // Release the previous focus first, otherwise FW_STATUS still reads "focused".
    af_command(AF_MAIN_RELEASE);
    af_wait_ack(200);
    af_command(AF_MAIN_SINGLE);
#endif
    return ESP_OK;
}

camera_af_result_t camera_af_wait(int timeout_ms)
{
    camera_af_result_t r = {.available = s_af_ok};
    if (!s_af_ok) return r;
    int64_t deadline = s_af_start_us + (int64_t)timeout_ms * 1000;
    int st = -1;
    while (true) {
        st = af_read(AF_FW_STATUS);
        if (st == AF_STATUS_FOCUSED) break;
        if (esp_timer_get_time() > deadline) break;
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    r.focused = st == AF_STATUS_FOCUSED;
    r.raw = (uint8_t)st;
    r.ms = (int)((esp_timer_get_time() - s_af_start_us) / 1000);
    if (r.focused) {
        ESP_LOGI(TAG, "AF focused in %d ms (%s)", r.ms, APP_AF_MODE_NAME);
    } else {
        ESP_LOGW(TAG, "AF not focused after %d ms (status 0x%02x), capturing anyway", r.ms,
                 r.raw);
    }
    return r;
}

camera_fb_t *camera_capture(void)
{
    int64_t t0 = esp_timer_get_time();
    // With fb_count = 1 and GRAB_LATEST, drop the buffered frame so the image is taken now.
    camera_fb_t *stale = esp_camera_fb_get();
    if (stale) esp_camera_fb_return(stale);
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
        ESP_LOGE(TAG, "capture failed");
        return NULL;
    }
    if (!jpeg_complete(fb)) {
        ESP_LOGE(TAG, "incomplete JPEG (%u bytes), missing EOI marker", (unsigned)fb->len);
        esp_camera_fb_return(fb);
        return NULL;
    }
    ESP_LOGI(TAG, "captured %ux%u, %u bytes in %lld ms", fb->width, fb->height,
             (unsigned)fb->len, (esp_timer_get_time() - t0) / 1000);
    return fb;
}

void camera_release(camera_fb_t *fb)
{
    if (fb) esp_camera_fb_return(fb);
}
