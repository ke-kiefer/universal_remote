#pragma once

#include <stdbool.h>

#include "driver/i2c_master.h"
#include "esp_err.h"
#include "lvgl.h"

// Initialize backlight, LCD, touch and the LVGL port. Returns the LVGL display.
esp_err_t board_display_init(lv_display_t **out_disp);

// Backlight brightness 0..100 %.
void board_backlight_set(int percent);

// I2C bus of touch and IMU (NULL before board_display_init).
i2c_master_bus_handle_t board_i2c_bus(void);

// LCD controller sleep mode on/off (light sleep). Backlight is switched off, not on.
void board_display_power(bool on);

// Before deep sleep: backlight off and held low, LCD controller in sleep mode.
void board_display_sleep(void);
