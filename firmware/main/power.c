#include "power.h"

#include "app_env.h"
#include "board.h"
#include "camera.h"
#include "esp_log.h"
#include "esp_lvgl_port.h"
#include "esp_pm.h"
#include "esp_timer.h"
#include "ui.h"
#include "wifi.h"

static const char *TAG = "power";

static power_state_t s_state = POWER_ACTIVE;
static int64_t s_since_us;

static const char *name(power_state_t s)
{
    return s == POWER_ACTIVE ? "active" : s == POWER_DIM ? "dim" : "idle";
}

static void set_state(power_state_t next)
{
    if (next == s_state) return;
    power_state_t prev = s_state;
    int64_t now = esp_timer_get_time();
    ESP_LOGI(TAG, "%s -> %s (after %lld s)", name(prev), name(next), (now - s_since_us) / 1000000);
    s_state = next;
    s_since_us = now;

    switch (next) {
    case POWER_ACTIVE:
        board_backlight_set(APP_BACKLIGHT_PCT);
        if (prev == POWER_IDLE) {
            if (APP_IDLE_WIFI_POWER_SAVE) wifi_set_power_save(false);
            camera_wake();
        }
        break;
    case POWER_DIM:
        ui_set_wake_guard(true);
        board_backlight_set(APP_DIM_PCT);
        break;
    case POWER_IDLE:
        ui_set_wake_guard(true);
        board_backlight_set(0);
        camera_sleep();
        if (APP_IDLE_WIFI_POWER_SAVE) wifi_set_power_save(true);
        break;
    }
}

void power_init(void)
{
    s_since_us = esp_timer_get_time();
#if CONFIG_PM_ENABLE
    // CPU drops to 80 MHz when nothing runs (APB stays at 80 MHz, so SPI/LEDC/I2C clocks
    // are unaffected). Automatic light sleep is not used yet.
    const esp_pm_config_t cfg = {
        .max_freq_mhz = CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ,
        .min_freq_mhz = 80,
        .light_sleep_enable = false,
    };
    esp_err_t err = esp_pm_configure(&cfg);
    ESP_LOGI(TAG, "DFS %d-%d MHz: %s, dim after %d s, idle after %d s", cfg.min_freq_mhz,
             cfg.max_freq_mhz, esp_err_to_name(err), APP_DIM_AFTER_S, APP_IDLE_AFTER_S);
#else
    ESP_LOGW(TAG, "CONFIG_PM_ENABLE off: no frequency scaling");
#endif
}

void power_update(void)
{
    uint32_t inactive_ms = 0;
    if (!lvgl_port_lock(100)) return;
    inactive_ms = lv_display_get_inactive_time(NULL);
    lvgl_port_unlock();

    power_state_t want = POWER_ACTIVE;
    if (APP_IDLE_AFTER_S > 0 && inactive_ms >= APP_IDLE_AFTER_S * 1000U) {
        want = POWER_IDLE;
    } else if (APP_DIM_AFTER_S > 0 && inactive_ms >= APP_DIM_AFTER_S * 1000U) {
        want = POWER_DIM;
    }
    // Only step down here; waking up is triggered by the touch (power_wake).
    if (want > s_state) set_state(want);
}

void power_kick(void)
{
    if (lvgl_port_lock(100)) {
        lv_display_trigger_activity(NULL);
        lvgl_port_unlock();
    }
}

void power_wake(void)
{
    power_kick();
    set_state(POWER_ACTIVE);
}

power_state_t power_state(void)
{
    return s_state;
}
