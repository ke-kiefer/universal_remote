#pragma once

#include <stdbool.h>

#include "esp_camera.h"
#include "esp_err.h"

// Initialize the OV5640 in JPEG mode with settings from .env, discard warm-up frames.
esp_err_t camera_init(void);

// Grab a fresh JPEG frame. Checks the JPEG end marker. Return it with camera_release().
camera_fb_t *camera_capture(void);

void camera_release(camera_fb_t *fb);
