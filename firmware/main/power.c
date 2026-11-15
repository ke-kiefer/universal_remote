#include "power.h"

#include "app_env.h"
#include "battery.h"
#include "board.h"
#include "board_pins.h"
#include "camera.h"
#include "driver/gpio.h"
#include "driver/rtc_io.h"
#include "driver/usb_serial_jtag.h"
#include "esp_log.h"
#include "esp_lvgl_port.h"
#include "esp_pm.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "imu.h"
#include "ui.h"
#include "wifi.h"

static const char *TAG = "power";

#define BATTERY_CHECK_US (30LL * 60 * 1000000)  // battery check interval while asleep

static power_state_t s_state = POWER_ACTIVE;
static int64_t s_since_us;
static bool s_usb_skip_logged;
static int s_low_count;

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

static const char *wake_cause(void)
{
    uint32_t causes = esp_sleep_get_wakeup_causes() & ~BIT(ESP_SLEEP_WAKEUP_UNDEFINED);
    if (causes & BIT(ESP_SLEEP_WAKEUP_EXT0)) return "BOOT key";
    return causes ? "other" : "power on / reset";
}

void power_init(void)
{
    s_since_us = esp_timer_get_time();
    ESP_LOGI(TAG, "wake-up cause: %s", wake_cause());
    rtc_gpio_deinit(BOARD_PIN_BOOT_KEY);  // was routed to RTC for the deep sleep wake-up
    imu_init();

    // Wake-up inputs for light sleep: the touch INT pulses low on a touch, the BOOT key is
    // low while pressed. Both read high otherwise.
    const gpio_config_t in = {
        .pin_bit_mask = (1ULL << BOARD_TOUCH_PIN_INT_WAKE) | (1ULL << BOARD_PIN_BOOT_KEY),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&in);

#if CONFIG_PM_ENABLE
    // CPU drops to 80 MHz when nothing runs (APB stays at 80 MHz, so SPI/LEDC/I2C clocks
    // are unaffected). Light sleep is entered explicitly (power_update), not automatically.
    const esp_pm_config_t cfg = {
        .max_freq_mhz = CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ,
        .min_freq_mhz = 80,
        .light_sleep_enable = false,
    };
    esp_err_t err = esp_pm_configure(&cfg);
    ESP_LOGI(TAG, "DFS %d-%d MHz: %s", cfg.min_freq_mhz, cfg.max_freq_mhz, esp_err_to_name(err));
#endif
    ESP_LOGI(TAG, "dim after %d s, idle after %d s, sleep after %d s (on USB: %s), "
             "battery off below %.2f V",
             APP_DIM_AFTER_S, APP_IDLE_AFTER_S, APP_SLEEP_AFTER_S,
             APP_SLEEP_ON_USB ? "yes" : "no", APP_BATTERY_OFF_V);
}

bool power_usb_host_connected(void)
{
    return usb_serial_jtag_is_connected();
}

void power_off(const char *reason)
{
    ESP_LOGI(TAG, "off (%s), wake by BOOT key", reason);
    wifi_suspend();
    camera_power_off();
    board_display_sleep();

    // BOOT key: active low; keep a pull-up on during deep sleep.
    esp_sleep_enable_ext0_wakeup(BOARD_PIN_BOOT_KEY, 0);
    rtc_gpio_pullup_en(BOARD_PIN_BOOT_KEY);
    rtc_gpio_pulldown_dis(BOARD_PIN_BOOT_KEY);
    esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH, ESP_PD_OPTION_ON);
    vTaskDelay(pdMS_TO_TICKS(50));  // flush the log
    esp_deep_sleep_start();
}

// Light sleep until a touch or the BOOT key. Wakes every BATTERY_CHECK_US to check the
// battery and goes back to sleep unless it is empty (two low readings in a row).
static void light_sleep(void)
{
    int64_t t0 = esp_timer_get_time();
    ESP_LOGI(TAG, "sleep (light), wake by touch or BOOT key");
    wifi_suspend();
    camera_sleep();  // standby keeps the AF firmware
    board_display_power(false);
    vTaskDelay(pdMS_TO_TICKS(20));  // let the log drain

    gpio_wakeup_enable(BOARD_TOUCH_PIN_INT_WAKE, GPIO_INTR_LOW_LEVEL);
    gpio_wakeup_enable(BOARD_PIN_BOOT_KEY, GPIO_INTR_LOW_LEVEL);
    esp_sleep_enable_gpio_wakeup();
    esp_sleep_enable_timer_wakeup(BATTERY_CHECK_US);

    while (true) {
        esp_light_sleep_start();
        uint32_t causes = esp_sleep_get_wakeup_causes();
        if (causes & BIT(ESP_SLEEP_WAKEUP_GPIO) || !(causes & BIT(ESP_SLEEP_WAKEUP_TIMER))) {
            break;
        }
        float v = battery_voltage();
        ESP_LOGI(TAG, "sleep: battery %.2f V", v);
        if (power_check_battery(v)) power_off("battery low");
    }

    esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_TIMER);
    esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_GPIO);
    gpio_wakeup_disable(BOARD_TOUCH_PIN_INT_WAKE);
    gpio_wakeup_disable(BOARD_PIN_BOOT_KEY);
    ESP_LOGI(TAG, "woke up after %lld s", (esp_timer_get_time() - t0) / 1000000);

    board_display_power(true);
    wifi_resume();
    // The wake guard is still shown, so the waking touch does not press a button.
    power_wake();
}

void power_update(void)
{
    uint32_t inactive_ms = 0;
    if (!lvgl_port_lock(100)) return;
    inactive_ms = lv_display_get_inactive_time(NULL);
    lvgl_port_unlock();

    if (APP_SLEEP_AFTER_S > 0 && inactive_ms >= APP_SLEEP_AFTER_S * 1000U) {
        if (APP_SLEEP_ON_USB || !power_usb_host_connected()) {
            light_sleep();
            return;
        }
        if (!s_usb_skip_logged) {
            s_usb_skip_logged = true;
            ESP_LOGI(TAG, "sleep skipped: USB host connected (SLEEP_ON_USB=0)");
        }
    } else {
        s_usb_skip_logged = false;
    }

    power_state_t want = POWER_ACTIVE;
    if (APP_IDLE_AFTER_S > 0 && inactive_ms >= APP_IDLE_AFTER_S * 1000U) {
        want = POWER_IDLE;
    } else if (APP_DIM_AFTER_S > 0 && inactive_ms >= APP_DIM_AFTER_S * 1000U) {
        want = POWER_DIM;
    }
    // Only step down here; waking up is triggered by the touch (power_wake).
    if (want > s_state) set_state(want);
}

bool power_check_battery(float v)
{
    if (v < 0 || APP_BATTERY_OFF_V <= 0) return false;
    // On a PC the board runs from USB; the ADC then shows the charging battery anyway.
    if (v >= APP_BATTERY_OFF_V || power_usb_host_connected()) {
        s_low_count = 0;
        return false;
    }
    ESP_LOGW(TAG, "battery low: %.2f V (%d)", v, s_low_count + 1);
    return ++s_low_count >= 2;
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
