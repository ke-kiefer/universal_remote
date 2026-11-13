#include "camera.h"

#include "app_env.h"
#include "board_pins.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "camera";

#define WARMUP_FRAMES 2

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

    // Let exposure and white balance settle.
    for (int i = 0; i < WARMUP_FRAMES; i++) {
        camera_fb_t *fb = esp_camera_fb_get();
        if (fb) esp_camera_fb_return(fb);
    }
    ESP_LOGI(TAG, "ready after %lld ms", (esp_timer_get_time() - t0) / 1000);
    return ESP_OK;
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
