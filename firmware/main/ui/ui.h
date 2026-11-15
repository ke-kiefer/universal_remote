// LVGL user interface. All public functions take the LVGL lock themselves and may be
// called from any task except the LVGL task. User actions are reported via the callback.
#pragma once

#include <stdbool.h>

#include "api_client.h"

typedef enum {
    UI_EV_IDENTIFY,     // "Erkennen" pressed
    UI_EV_TRAIN_PHOTO,  // "Foto" pressed in training mode
    UI_EV_ACTION,       // action button on the result screen, arg = action index
    UI_EV_CHOOSE,       // choose screen: arg = candidate index, -1 = "Keins davon"
    UI_EV_WAKE,         // touch while the wake guard was shown (dimmed/idle)
} ui_event_t;

typedef void (*ui_event_cb_t)(ui_event_t ev, int arg);

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

// Invisible touch catcher over everything: the first touch after dimming only wakes the
// device (UI_EV_WAKE) instead of pressing a button. Hides itself when that touch ends.
void ui_set_wake_guard(bool on);

// Short message at the bottom of the screen (auto-hides).
void ui_toast(const char *text, bool error);

void ui_show_home(void);

// Full-screen progress (spinner + text) while identifying.
void ui_show_busy(const char *text);

// Result screen: name, area, state text, confidence and the server's action buttons.
void ui_show_result(const api_entity_view_t *view);
// Refresh state text and actions after an action (stays on the result screen).
void ui_update_result(const api_entity_view_t *view);
// Lock/unlock the action buttons while a request is running.
void ui_result_busy(bool busy);

// Choose screen for uncertain matches: up to API_MAX_CANDIDATES buttons + "Keins davon".
void ui_show_choose(const api_entity_t *cands, int n);

// Training screen: busy state with progress text, then the result.
void ui_train_busy(const char *text);
void ui_train_done(bool ok, int sent_count, const char *detail);
