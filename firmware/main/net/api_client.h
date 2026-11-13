#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

typedef struct {
    int entities;
    int images;
} api_health_t;

// GET /api/health. Returns ESP_OK and fills `out` on success.
esp_err_t api_health(api_health_t *out);

// POST /api/capture (training mode). Streams the JPEG straight from `jpeg`.
// `meta` is an optional query string with diagnostics (e.g. "af=ok&af_ms=420"), logged by
// the server. On success copies the server's image id into id_out.
esp_err_t api_capture(const uint8_t *jpeg, size_t len, const char *meta, char *id_out,
                      size_t id_len);
