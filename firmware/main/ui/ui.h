// LVGL user interface. All public functions take the LVGL lock themselves and may be
// called from any task except the LVGL task. User actions are reported via the callback.
#pragma once

#include <stdbool.h>

typedef enum {
    UI_EV_IDENTIFY,     // "Erkennen" pressed
    UI_EV_TRAIN_PHOTO,  // "Foto" pressed in training mode
} ui_event_t;

typedef void (*ui_event_cb_t)(ui_event_t ev);

typedef struct {
    bool wifi;
    int rssi;
    enum { UI_SRV_UNKNOWN, UI_SRV_OK, UI_SRV_OFFLINE, UI_SRV_AUTH } server;
    int entities;
    int images;
    float battery_v;  // < 0: unknown
    int battery_pct;
} ui_status_t;

// Create fonts, status bar and screens; show the home screen.
void ui_init(ui_event_cb_t cb);

void ui_set_status(const ui_status_t *st);

// Short message at the bottom of the screen (auto-hides).
void ui_toast(const char *text, bool error);

// Training screen: busy state with progress text, then the result.
void ui_train_busy(const char *text);
void ui_train_done(bool ok, int sent_count, const char *detail);
