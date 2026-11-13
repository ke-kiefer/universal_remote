// HTTP client for the recognition server (see API contract in CLAUDE.md).
// Never call from the LVGL task. The firmware only displays what the server sends:
// names, state text and action labels come ready-made from the server.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#define API_MAX_ACTIONS 6
#define API_MAX_CANDIDATES 3

typedef struct {
    int entities;
    int images;
} api_health_t;

typedef struct {
    char id[24];
    char label[32];
} api_action_t;

typedef struct {
    char entity_id[64];
    char name[64];
    char area[40];
    float confidence;
} api_entity_t;

// State + actions of one entity (identify result, action response, entity refresh).
typedef struct {
    api_entity_t entity;
    char state_text[96];
    int n_actions;
    api_action_t actions[API_MAX_ACTIONS];
} api_entity_view_t;

typedef enum { API_MATCH, API_UNCERTAIN, API_UNKNOWN } api_status_t;

typedef struct {
    api_status_t status;
    char capture_id[48];
    api_entity_view_t best;  // valid unless API_UNKNOWN
    int n_candidates;
    api_entity_t candidates[API_MAX_CANDIDATES];
    int server_ms;  // server-side processing time
} api_identify_t;

// GET /api/health. ESP_ERR_INVALID_ARG = wrong API key.
esp_err_t api_health(api_health_t *out);

// POST /api/capture (training mode). Streams the JPEG straight from `jpeg`.
// `meta` is an optional query string with diagnostics (e.g. "af=ok&af_ms=420"), logged by
// the server. On success copies the server's image id into id_out.
esp_err_t api_capture(const uint8_t *jpeg, size_t len, const char *meta, char *id_out,
                      size_t id_len);

// POST /api/identify. Streams the JPEG, parses the result.
esp_err_t api_identify(const uint8_t *jpeg, size_t len, const char *meta, api_identify_t *out);

// POST /api/action {entity_id, action}. On success `view` holds the new state and actions.
esp_err_t api_action(const char *entity_id, const char *action, api_entity_view_t *view);

// POST /api/feedback {capture_id, entity_id}: the capture becomes a training image.
esp_err_t api_feedback(const char *capture_id, const char *entity_id);

// GET /api/entity/{id}: current state and actions.
esp_err_t api_entity(const char *entity_id, api_entity_view_t *view);
