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

// Timeouts from CLAUDE.md: identify/capture 8 s (upload), action 3 s.
#define TIMEOUT_HEALTH_MS 3000
#define TIMEOUT_UPLOAD_MS 8000
#define TIMEOUT_ACTION_MS 3000
#define RESP_MAX 8192

// Perform a request with an optional body, read the response into a heap buffer.
// Caller frees *out_body. Returns the HTTP status or -1 on transport error.
static int request(const char *path, esp_http_client_method_t method, const char *content_type,
                   const void *body, size_t body_len, int timeout_ms, char **out_body)
{
    char url[192];
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

// --- JSON helpers -----------------------------------------------------------------------

static void copy_str(const cJSON *obj, const char *key, char *dst, size_t len)
{
    const cJSON *v = cJSON_GetObjectItem(obj, key);
    strlcpy(dst, cJSON_IsString(v) ? v->valuestring : "", len);
}

static void parse_entity(const cJSON *obj, api_entity_t *e)
{
    memset(e, 0, sizeof(*e));
    if (!cJSON_IsObject(obj)) return;
    copy_str(obj, "entity_id", e->entity_id, sizeof(e->entity_id));
    copy_str(obj, "name", e->name, sizeof(e->name));
    copy_str(obj, "area", e->area, sizeof(e->area));
    const cJSON *c = cJSON_GetObjectItem(obj, "confidence");
    e->confidence = cJSON_IsNumber(c) ? (float)c->valuedouble : 0.0f;
}

// state_text + actions from a response object.
static void parse_view(const cJSON *root, api_entity_view_t *v)
{
    copy_str(root, "state_text", v->state_text, sizeof(v->state_text));
    v->n_actions = 0;
    const cJSON *acts = cJSON_GetObjectItem(root, "actions");
    const cJSON *a;
    cJSON_ArrayForEach(a, acts)
    {
        if (v->n_actions >= API_MAX_ACTIONS) break;
        api_action_t *dst = &v->actions[v->n_actions++];
        copy_str(a, "id", dst->id, sizeof(dst->id));
        copy_str(a, "label", dst->label, sizeof(dst->label));
    }
}

static esp_err_t post_json(const char *path, cJSON *body, int timeout_ms, cJSON **out)
{
    char *txt = cJSON_PrintUnformatted(body);
    if (!txt) return ESP_ERR_NO_MEM;
    char *resp = NULL;
    int status =
        request(path, HTTP_METHOD_POST, "application/json", txt, strlen(txt), timeout_ms, &resp);
    free(txt);
    esp_err_t ret = ESP_FAIL;
    if (status == 200 && resp) {
        *out = cJSON_Parse(resp);
        ret = *out ? ESP_OK : ESP_FAIL;
    } else if (resp) {
        ESP_LOGW(TAG, "%s failed (%d): %.160s", path, status, resp);
    }
    free(resp);
    return ret;
}

// --- endpoints ----------------------------------------------------------------------------

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

esp_err_t api_capture(const uint8_t *jpeg, size_t len, const char *meta, char *id_out,
                      size_t id_len)
{
    char path[160];
    snprintf(path, sizeof(path), "/api/capture?device=%s%s%s", APP_DEVICE_NAME,
             meta && *meta ? "&" : "", meta ? meta : "");
    char *body = NULL;
    int status =
        request(path, HTTP_METHOD_POST, "image/jpeg", jpeg, len, TIMEOUT_UPLOAD_MS, &body);
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

esp_err_t api_identify(const uint8_t *jpeg, size_t len, const char *meta, api_identify_t *out)
{
    memset(out, 0, sizeof(*out));
    char path[160];
    snprintf(path, sizeof(path), "/api/identify?device=%s%s%s", APP_DEVICE_NAME,
             meta && *meta ? "&" : "", meta ? meta : "");
    char *body = NULL;
    int status =
        request(path, HTTP_METHOD_POST, "image/jpeg", jpeg, len, TIMEOUT_UPLOAD_MS, &body);
    esp_err_t ret = ESP_FAIL;
    cJSON *j = (status == 200 && body) ? cJSON_Parse(body) : NULL;
    if (j) {
        char st[16];
        copy_str(j, "status", st, sizeof(st));
        out->status = !strcmp(st, "match")       ? API_MATCH
                      : !strcmp(st, "uncertain") ? API_UNCERTAIN
                                                 : API_UNKNOWN;
        copy_str(j, "capture_id", out->capture_id, sizeof(out->capture_id));
        parse_entity(cJSON_GetObjectItem(j, "best"), &out->best.entity);
        parse_view(j, &out->best);
        const cJSON *c;
        cJSON_ArrayForEach(c, cJSON_GetObjectItem(j, "candidates"))
        {
            if (out->n_candidates >= API_MAX_CANDIDATES) break;
            parse_entity(c, &out->candidates[out->n_candidates++]);
        }
        const cJSON *timing = cJSON_GetObjectItem(j, "timing_ms");
        const cJSON *total = timing ? cJSON_GetObjectItem(timing, "total") : NULL;
        out->server_ms = cJSON_IsNumber(total) ? total->valueint : -1;
        cJSON_Delete(j);
        ret = ESP_OK;
    } else if (body) {
        ESP_LOGW(TAG, "identify failed (%d): %.120s", status, body);
    }
    free(body);
    return ret;
}

esp_err_t api_action(const char *entity_id, const char *action, api_entity_view_t *view)
{
    cJSON *req = cJSON_CreateObject();
    cJSON_AddStringToObject(req, "entity_id", entity_id);
    cJSON_AddStringToObject(req, "action", action);
    cJSON *resp = NULL;
    esp_err_t err = post_json("/api/action", req, TIMEOUT_ACTION_MS, &resp);
    cJSON_Delete(req);
    if (err == ESP_OK) {
        parse_view(resp, view);
        cJSON_Delete(resp);
    }
    return err;
}

esp_err_t api_feedback(const char *capture_id, const char *entity_id)
{
    cJSON *req = cJSON_CreateObject();
    cJSON_AddStringToObject(req, "capture_id", capture_id);
    cJSON_AddStringToObject(req, "entity_id", entity_id);
    cJSON *resp = NULL;
    esp_err_t err = post_json("/api/feedback", req, TIMEOUT_ACTION_MS, &resp);
    cJSON_Delete(req);
    cJSON_Delete(resp);
    return err;
}

esp_err_t api_entity(const char *entity_id, api_entity_view_t *view)
{
    char path[96];
    snprintf(path, sizeof(path), "/api/entity/%s", entity_id);
    char *body = NULL;
    int status = request(path, HTTP_METHOD_GET, NULL, NULL, 0, TIMEOUT_ACTION_MS, &body);
    esp_err_t ret = ESP_FAIL;
    cJSON *j = (status == 200 && body) ? cJSON_Parse(body) : NULL;
    if (j) {
        parse_entity(cJSON_GetObjectItem(j, "entity"), &view->entity);
        parse_view(j, view);
        cJSON_Delete(j);
        ret = ESP_OK;
    }
    free(body);
    return ret;
}
