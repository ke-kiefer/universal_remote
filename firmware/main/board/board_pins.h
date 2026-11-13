// Pin assignment for the Waveshare ESP32-S3-Touch-LCD-2.
//
// Source for all values: official Waveshare demo package
//   https://files.waveshare.com/wiki/ESP32-S3-Touch-LCD-2/ESP32-S3-Touch-LCD-2-Demo.zip
//   ESP-IDF/05_lvgl_camera/main/main.c   (LCD, touch I2C, camera, backlight)
//   ESP-IDF/04_lvgl_battery/main/main.c  (battery ADC)
// Retrieved 2026-11-13. Do not change without checking the schematic:
//   https://files.waveshare.com/wiki/ESP32-S3-Touch-LCD-2/ESP32-S3-Touch-LCD-2-SchDoc.pdf
#pragma once

#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "hal/adc_types.h"
#include "hal/i2c_types.h"
#include "hal/ledc_types.h"

// --- LCD: ST7789T3, 240x320, SPI -----------------------------------------------------------
#define BOARD_LCD_SPI_HOST      SPI2_HOST           // EXAMPLE_SPI_HOST
#define BOARD_LCD_PIN_SCLK      GPIO_NUM_39         // EXAMPLE_PIN_NUM_SCLK
#define BOARD_LCD_PIN_MOSI      GPIO_NUM_38         // EXAMPLE_PIN_NUM_MOSI
#define BOARD_LCD_PIN_MISO      GPIO_NUM_40         // EXAMPLE_PIN_NUM_MISO (shared with SD card)
#define BOARD_LCD_PIN_CS        GPIO_NUM_45         // EXAMPLE_PIN_NUM_LCD_CS
#define BOARD_LCD_PIN_DC        GPIO_NUM_42         // EXAMPLE_PIN_NUM_LCD_DC
#define BOARD_LCD_PIN_RST       GPIO_NUM_NC         // EXAMPLE_PIN_NUM_LCD_RST (-1, not connected)
#define BOARD_LCD_PIN_BL        GPIO_NUM_1          // EXAMPLE_PIN_NUM_BK_LIGHT (LEDC PWM)
#define BOARD_LCD_PIXEL_CLOCK   (80 * 1000 * 1000)  // EXAMPLE_LCD_PIXEL_CLOCK_HZ
#define BOARD_LCD_H_RES         240                 // EXAMPLE_LCD_H_RES
#define BOARD_LCD_V_RES         320                 // EXAMPLE_LCD_V_RES
#define BOARD_LCD_INVERT_COLOR  1                   // esp_lcd_panel_invert_color(.., true)

// --- Touch: CST816D on I2C0 (also QMI8658 IMU on this bus) ---------------------------------
#define BOARD_TOUCH_I2C_PORT    I2C_NUM_0           // EXAMPLE_I2C_NUM
#define BOARD_TOUCH_PIN_SDA     GPIO_NUM_48         // EXAMPLE_PIN_NUM_I2C_SDA
#define BOARD_TOUCH_PIN_SCL     GPIO_NUM_47         // EXAMPLE_PIN_NUM_I2C_SCL
#define BOARD_TOUCH_PIN_INT     GPIO_NUM_NC         // demo: int_gpio_num = -1
#define BOARD_TOUCH_PIN_RST     GPIO_NUM_NC         // demo: rst_gpio_num = -1

// --- Camera: DVP (OV5640 on the 24-pin FPC connector) --------------------------------------
// The demo names data lines Y2..Y9 (= D0..D7).
#define BOARD_CAM_PIN_PWDN      GPIO_NUM_17         // PWDN_GPIO_NUM
#define BOARD_CAM_PIN_RESET     GPIO_NUM_NC         // RESET_GPIO_NUM (-1, software reset)
#define BOARD_CAM_PIN_XCLK      GPIO_NUM_8          // XCLK_GPIO_NUM
#define BOARD_CAM_PIN_SIOD      GPIO_NUM_21         // SIOD_GPIO_NUM (SCCB data, own bus)
#define BOARD_CAM_PIN_SIOC      GPIO_NUM_16         // SIOC_GPIO_NUM (SCCB clock)
#define BOARD_CAM_PIN_D7        GPIO_NUM_2          // Y9_GPIO_NUM
#define BOARD_CAM_PIN_D6        GPIO_NUM_7          // Y8_GPIO_NUM
#define BOARD_CAM_PIN_D5        GPIO_NUM_10         // Y7_GPIO_NUM
#define BOARD_CAM_PIN_D4        GPIO_NUM_14         // Y6_GPIO_NUM
#define BOARD_CAM_PIN_D3        GPIO_NUM_11         // Y5_GPIO_NUM
#define BOARD_CAM_PIN_D2        GPIO_NUM_15         // Y4_GPIO_NUM
#define BOARD_CAM_PIN_D1        GPIO_NUM_13         // Y3_GPIO_NUM
#define BOARD_CAM_PIN_D0        GPIO_NUM_12         // Y2_GPIO_NUM
#define BOARD_CAM_PIN_VSYNC     GPIO_NUM_6          // VSYNC_GPIO_NUM
#define BOARD_CAM_PIN_HREF      GPIO_NUM_4          // HREF_GPIO_NUM
#define BOARD_CAM_PIN_PCLK      GPIO_NUM_9          // PCLK_GPIO_NUM

// --- Battery voltage -----------------------------------------------------------------------
// 04_lvgl_battery: ADC_UNIT_1, ADC_CHANNEL_4 (= GPIO5 on ESP32-S3), ADC_ATTEN_DB_12,
// voltage = adc_mv / 1000 * 3.0 (1:3 divider).
#define BOARD_BAT_ADC_UNIT      ADC_UNIT_1
#define BOARD_BAT_ADC_CHANNEL   ADC_CHANNEL_4
#define BOARD_BAT_DIVIDER       3.0f

// --- Peripheral allocation (ours, not from the demo) ---------------------------------------
// The demo uses LEDC channel 0 for both backlight and camera XCLK; we keep them apart.
#define BOARD_BL_LEDC_TIMER     LEDC_TIMER_0
#define BOARD_BL_LEDC_CHANNEL   LEDC_CHANNEL_0
#define BOARD_CAM_LEDC_TIMER    LEDC_TIMER_1
#define BOARD_CAM_LEDC_CHANNEL  LEDC_CHANNEL_1
#define BOARD_CAM_SCCB_PORT     I2C_NUM_1           // touch uses I2C_NUM_0
