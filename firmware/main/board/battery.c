// Battery voltage via ADC (pin and divider from the Waveshare demo 04_lvgl_battery).

#include "battery.h"

#include "board_pins.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_log.h"

static const char *TAG = "battery";

#define SAMPLES 8

static adc_oneshot_unit_handle_t s_adc;
static adc_cali_handle_t s_cali;

esp_err_t battery_init(void)
{
    const adc_oneshot_unit_init_cfg_t unit = {.unit_id = BOARD_BAT_ADC_UNIT};
    esp_err_t err = adc_oneshot_new_unit(&unit, &s_adc);
    if (err != ESP_OK) return err;
    const adc_oneshot_chan_cfg_t ch = {.atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_DEFAULT};
    err = adc_oneshot_config_channel(s_adc, BOARD_BAT_ADC_CHANNEL, &ch);
    if (err != ESP_OK) return err;
    const adc_cali_curve_fitting_config_t cali = {
        .unit_id = BOARD_BAT_ADC_UNIT,
        .chan = BOARD_BAT_ADC_CHANNEL,
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    if (adc_cali_create_scheme_curve_fitting(&cali, &s_cali) != ESP_OK) {
        ESP_LOGW(TAG, "no ADC calibration, voltage will be less accurate");
        s_cali = NULL;
    }
    ESP_LOGI(TAG, "battery voltage %.2f V", battery_voltage());
    return ESP_OK;
}

float battery_voltage(void)
{
    if (!s_adc) return -1.0f;
    int sum_mv = 0, n = 0;
    for (int i = 0; i < SAMPLES; i++) {
        int raw = 0, mv = 0;
        if (adc_oneshot_read(s_adc, BOARD_BAT_ADC_CHANNEL, &raw) != ESP_OK) continue;
        if (s_cali) {
            adc_cali_raw_to_voltage(s_cali, raw, &mv);
        } else {
            mv = raw * 3100 / 4095;
        }
        sum_mv += mv;
        n++;
    }
    if (!n) return -1.0f;
    return (sum_mv / (float)n) / 1000.0f * BOARD_BAT_DIVIDER;
}

int battery_percent(float v)
{
    int p = (int)((v - 3.3f) / (4.2f - 3.3f) * 100.0f + 0.5f);
    return p < 0 ? 0 : p > 100 ? 100 : p;
}
