#pragma once

#include "esp_err.h"
#include "lvgl.h"

// Initialize backlight, LCD, touch and the LVGL port. Returns the LVGL display.
esp_err_t board_display_init(lv_display_t **out_disp);

// Backlight brightness 0..100 %.
void board_backlight_set(int percent);
