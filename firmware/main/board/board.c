// Display (ST7789 over SPI), touch (CST816D over I2C) and LVGL port setup.
// Driver init follows the Waveshare demo 05_lvgl_camera, ported to ESP-IDF 6
// (i2c_master driver) and esp_lvgl_port / LVGL 9.

#include "board.h"

#include "board_pins.h"
#include "driver/i2c_master.h"
#include "driver/ledc.h"
#include "driver/spi_master.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_st7789.h"
#include "esp_check.h"
#include "esp_lcd_touch_cst816s.h"
#include "esp_log.h"
#include "esp_lvgl_port.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "board";

#define LCD_DRAW_LINES 40  // LVGL draw buffer height in lines

static i2c_master_bus_handle_t s_i2c_bus;
static esp_lcd_panel_handle_t s_panel;

static void backlight_init(void)
{
    gpio_hold_dis(BOARD_LCD_PIN_BL);  // held low during deep sleep
    const ledc_timer_config_t timer = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .duty_resolution = LEDC_TIMER_10_BIT,
        .timer_num = BOARD_BL_LEDC_TIMER,
        .freq_hz = 10000,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    ESP_ERROR_CHECK(ledc_timer_config(&timer));
    const ledc_channel_config_t ch = {
        .gpio_num = BOARD_LCD_PIN_BL,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel = BOARD_BL_LEDC_CHANNEL,
        .timer_sel = BOARD_BL_LEDC_TIMER,
        .duty = 0,
    };
    ESP_ERROR_CHECK(ledc_channel_config(&ch));
}

void board_backlight_set(int percent)
{
    if (percent < 0) percent = 0;
    if (percent > 100) percent = 100;
    uint32_t duty = (1023 * percent) / 100;
    ledc_set_duty(LEDC_LOW_SPEED_MODE, BOARD_BL_LEDC_CHANNEL, duty);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, BOARD_BL_LEDC_CHANNEL);
}

static esp_err_t lcd_init(esp_lcd_panel_io_handle_t *out_io, esp_lcd_panel_handle_t *out_panel)
{
    const spi_bus_config_t bus = {
        .sclk_io_num = BOARD_LCD_PIN_SCLK,
        .mosi_io_num = BOARD_LCD_PIN_MOSI,
        .miso_io_num = BOARD_LCD_PIN_MISO,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = BOARD_LCD_H_RES * LCD_DRAW_LINES * sizeof(uint16_t),
    };
    ESP_RETURN_ON_ERROR(spi_bus_initialize(BOARD_LCD_SPI_HOST, &bus, SPI_DMA_CH_AUTO), TAG, "spi bus");

    const esp_lcd_panel_io_spi_config_t io_cfg = {
        .dc_gpio_num = BOARD_LCD_PIN_DC,
        .cs_gpio_num = BOARD_LCD_PIN_CS,
        .pclk_hz = BOARD_LCD_PIXEL_CLOCK,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
        .spi_mode = 0,
        .trans_queue_depth = 10,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)BOARD_LCD_SPI_HOST,
                                                 &io_cfg, out_io), TAG, "panel io");

    const esp_lcd_panel_dev_config_t panel_cfg = {
        .reset_gpio_num = BOARD_LCD_PIN_RST,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_st7789(*out_io, &panel_cfg, out_panel), TAG, "st7789");
    esp_lcd_panel_reset(*out_panel);
    esp_lcd_panel_init(*out_panel);
    esp_lcd_panel_mirror(*out_panel, false, false);
    esp_lcd_panel_swap_xy(*out_panel, false);
    esp_lcd_panel_invert_color(*out_panel, BOARD_LCD_INVERT_COLOR);
    esp_lcd_panel_disp_on_off(*out_panel, true);
    return ESP_OK;
}

static esp_err_t touch_init(esp_lcd_touch_handle_t *out_tp)
{
    i2c_master_bus_handle_t bus = NULL;
    const i2c_master_bus_config_t bus_cfg = {
        .i2c_port = BOARD_TOUCH_I2C_PORT,
        .sda_io_num = BOARD_TOUCH_PIN_SDA,
        .scl_io_num = BOARD_TOUCH_PIN_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    ESP_RETURN_ON_ERROR(i2c_new_master_bus(&bus_cfg, &bus), TAG, "i2c bus");
    s_i2c_bus = bus;

    esp_lcd_panel_io_handle_t io = NULL;
    esp_lcd_panel_io_i2c_config_t io_cfg = ESP_LCD_TOUCH_IO_I2C_CST816S_CONFIG();
    io_cfg.scl_speed_hz = 400000;
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_i2c(bus, &io_cfg, &io), TAG, "touch io");

    const esp_lcd_touch_config_t tp_cfg = {
        .x_max = BOARD_LCD_H_RES,
        .y_max = BOARD_LCD_V_RES,
        .rst_gpio_num = BOARD_TOUCH_PIN_RST,
        .int_gpio_num = BOARD_TOUCH_PIN_INT,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_touch_new_i2c_cst816s(io, &tp_cfg, out_tp), TAG, "cst816");
    return ESP_OK;
}

esp_err_t board_display_init(lv_display_t **out_disp)
{
    backlight_init();

    esp_lcd_panel_io_handle_t io = NULL;
    esp_lcd_panel_handle_t panel = NULL;
    ESP_RETURN_ON_ERROR(lcd_init(&io, &panel), TAG, "lcd");
    s_panel = panel;

    esp_lcd_touch_handle_t tp = NULL;
    esp_err_t touch_err = touch_init(&tp);
    if (touch_err != ESP_OK) {
        ESP_LOGE(TAG, "touch init failed: %s", esp_err_to_name(touch_err));
    }

    const lvgl_port_cfg_t lvgl_cfg = ESP_LVGL_PORT_INIT_CONFIG();
    ESP_RETURN_ON_ERROR(lvgl_port_init(&lvgl_cfg), TAG, "lvgl port");

    const lvgl_port_display_cfg_t disp_cfg = {
        .io_handle = io,
        .panel_handle = panel,
        .buffer_size = BOARD_LCD_H_RES * LCD_DRAW_LINES,
        .double_buffer = true,
        .hres = BOARD_LCD_H_RES,
        .vres = BOARD_LCD_V_RES,
        .monochrome = false,
        .color_format = LV_COLOR_FORMAT_RGB565,
        .rotation = {.swap_xy = false, .mirror_x = false, .mirror_y = false},
        .flags = {.buff_dma = true, .swap_bytes = true},
    };
    lv_display_t *disp = lvgl_port_add_disp(&disp_cfg);
    if (!disp) return ESP_FAIL;

    if (tp) {
        const lvgl_port_touch_cfg_t touch_cfg = {.disp = disp, .handle = tp};
        if (!lvgl_port_add_touch(&touch_cfg)) ESP_LOGE(TAG, "lvgl touch add failed");
    }

    board_backlight_set(80);
    ESP_LOGI(TAG, "display %dx%d ready, touch %s", BOARD_LCD_H_RES, BOARD_LCD_V_RES,
             tp ? "ok" : "MISSING");
    *out_disp = disp;
    return ESP_OK;
}

i2c_master_bus_handle_t board_i2c_bus(void)
{
    return s_i2c_bus;
}

void board_display_power(bool on)
{
    if (!s_panel) return;
    if (on) {
        esp_lcd_panel_disp_sleep(s_panel, false);  // ST7789 needs ~120 ms after sleep out
        vTaskDelay(pdMS_TO_TICKS(120));
    } else {
        board_backlight_set(0);
        esp_lcd_panel_disp_sleep(s_panel, true);
    }
}

void board_display_sleep(void)
{
    board_backlight_set(0);
    ledc_stop(LEDC_LOW_SPEED_MODE, BOARD_BL_LEDC_CHANNEL, 0);
    gpio_hold_en(BOARD_LCD_PIN_BL);  // RTC GPIO: stays low in deep sleep (no glowing)
    if (s_panel) esp_lcd_panel_disp_sleep(s_panel, true);
}
