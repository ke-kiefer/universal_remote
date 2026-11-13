// HTTP client for the recognition server. Never call from the LVGL task.

#include "api_client.h"

#include <stdlib.h>
#include <string.h>

#include "app_env.h"
#include "cJSON.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_timer.h"

static const char *TAG = "api";

#define TIMEOUT_HEALTH_MS 3000
#define TIMEOUT_CAPTURE_MS 8000
#define RESP_MAX 4096

// Perform a request with an optional body, read the JSON response into a heap buffer.
// Caller frees *out_body. Returns the HTTP status or -1 on transport error.
static int request(const char *path, esp_http_client_method_t method, const char *content_type,
                   const void *body, size_t body_len, int timeout_ms, char **out_body)
{
    char url[160];
    snprintf(url, sizeof(url), "%s%s", APP_SERVER_URL, path);
    esp_http_client_config_t cfg = {
        .url = url,
        .method = method,
        .timeout_ms = timeout_ms,
        .buffer_size = 1024,
    };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) return -1;
    esp_http_client_set_header(c, "X-Api-Key", APP_API_KEY);
    if (content_type) esp_http_client_set_header(c, "Content-Type", content_type);

    int status = -1;
    *out_body = NULL;
    int64_t t0 = esp_timer_get_time();
    esp_err_t err = esp_http_client_open(c, body_len);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "%s: connect failed: %s", path, esp_err_to_name(err));
        goto done;
    }
    if (body_len && esp_http_client_write(c, body, body_len) != (int)body_len) {
        ESP_LOGW(TAG, "%s: write failed", path);
        goto done;
    }
    if (esp_http_client_fetch_headers(c) < 0) {
        ESP_LOGW(TAG, "%s: no response", path);
        goto done;
    }
    status = esp_http_client_get_status_code(c);
    char *buf = malloc(RESP_MAX);
    if (!buf) goto done;
    int total = 0, n;
    while (total < RESP_MAX - 1 &&
           (n = esp_http_client_read(c, buf + total, RESP_MAX - 1 - total)) > 0) {
        total += n;
    }
    buf[total] = '\0';
    *out_body = buf;
    ESP_LOGI(TAG, "%s -> %d (%d bytes, %lld ms)", path, status, total,
             (esp_timer_get_time() - t0) / 1000);
done:
    esp_http_client_cleanup(c);
    return status;
}

esp_err_t api_health(api_health_t *out)
{
    char *body = NULL;
    int status = request("/api/health", HTTP_METHOD_GET, NULL, NULL, 0, TIMEOUT_HEALTH_MS, &body);
    esp_err_t ret = ESP_FAIL;
    if (status == 200 && body) {
        cJSON *j = cJSON_Parse(body);
        if (j) {
            const cJSON *e = cJSON_GetObjectItem(j, "entities");
            const cJSON *i = cJSON_GetObjectItem(j, "images");
            out->entities = cJSON_IsNumber(e) ? e->valueint : 0;
            out->images = cJSON_IsNumber(i) ? i->valueint : 0;
            ret = ESP_OK;
            cJSON_Delete(j);
        }
    } else if (status == 401) {
        ESP_LOGE(TAG, "server rejected API key (401) – check API_KEY in firmware/.env and server/.env");
        ret = ESP_ERR_INVALID_ARG;
    }
    free(body);
    return ret;
}

esp_err_t api_capture(const uint8_t *jpeg, size_t len, char *id_out, size_t id_len)
{
    char path[64];
    snprintf(path, sizeof(path), "/api/capture?device=%s", APP_DEVICE_NAME);
    char *body = NULL;
    int status = request(path, HTTP_METHOD_POST, "image/jpeg", jpeg, len, TIMEOUT_CAPTURE_MS,
                         &body);
    esp_err_t ret = ESP_FAIL;
    if (status == 201 && body) {
        cJSON *j = cJSON_Parse(body);
        const cJSON *id = j ? cJSON_GetObjectItem(j, "id") : NULL;
        if (cJSON_IsString(id)) {
            strlcpy(id_out, id->valuestring, id_len);
            ret = ESP_OK;
        }
        cJSON_Delete(j);
    } else if (body) {
        ESP_LOGW(TAG, "capture rejected (%d): %.120s", status, body);
    }
    free(body);
    return ret;
}
