#pragma once

#include <stdbool.h>

#include "esp_camera.h"
#include "esp_err.h"

// Initialize the OV5640 in JPEG mode with settings from .env, discard warm-up frames.
esp_err_t camera_init(void);

// Software standby (registers and AF firmware are kept, XCLK stopped) and wake-up.
// Capture/AF functions wake the camera automatically if needed.
void camera_sleep(void);
void camera_wake(void);

// Autofocus (OV5640 AF firmware, loaded in camera_init).
typedef struct {
    bool available;  // AF firmware loaded
    bool focused;    // sensor reported "focused" before the timeout
    uint8_t raw;     // last raw FW status (0x10 = focused, 0x70 = idle)
    int ms;          // time from trigger until focused / timeout
} camera_af_result_t;

// Start a single AF cycle (non-blocking apart from the command handshake).
esp_err_t camera_af_start(void);

// Wait until the AF cycle finished or timeout_ms passed. Capture anyway afterwards.
camera_af_result_t camera_af_wait(int timeout_ms);

// Grab a fresh JPEG frame. Checks the JPEG end marker. Return it with camera_release().
camera_fb_t *camera_capture(void);

void camera_release(camera_fb_t *fb);
