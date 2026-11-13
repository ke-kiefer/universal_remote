// Screens (240x320 portrait):
//   status bar (top layer, always visible): WLAN, server, battery
//   HOME:  big "Erkennen" button, "Training" button, index info
//   TRAIN: back button, big "Foto" button, counter, last result
// Text uses Montserrat via Tiny TTF (umlauts); symbols fall back to LVGL's built-in fonts.

#include "ui.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_lvgl_port.h"
#include "lvgl.h"

static const char *TAG = "ui";

extern const uint8_t font_ttf_start[] asm("_binary_Montserrat_SemiBold_latin1_ttf_start");
extern const uint8_t font_ttf_end[] asm("_binary_Montserrat_SemiBold_latin1_ttf_end");

#define COLOR_BG 0x101418
#define COLOR_BAR 0x0A0D10
#define COLOR_TEXT 0xECEFF1
#define COLOR_MUTED 0x90A4AE
#define COLOR_ACCENT 0x2F80ED
#define COLOR_TRAIN 0xE07B24
#define COLOR_SECONDARY 0x37474F
#define COLOR_OK 0x43A047
#define COLOR_WARN 0xFDD835
#define COLOR_WEAK 0xFB8C00
#define COLOR_ERR 0xE53935

#define BAR_H 26
#define TOAST_MS 2500

static ui_event_cb_t s_cb;
static lv_font_t *s_font_s, *s_font_m, *s_font_l;

static lv_obj_t *s_icon_server, *s_icon_wifi, *s_icon_battery;
static lv_obj_t *s_home, *s_train;
static lv_obj_t *s_home_info;
static lv_obj_t *s_train_btn, *s_train_btn_label, *s_train_back;
static lv_obj_t *s_train_count, *s_train_detail, *s_train_spinner;
static lv_obj_t *s_toast;
static lv_timer_t *s_toast_timer;

// --- helpers ------------------------------------------------------------------------------

static lv_font_t *make_font(int px, const lv_font_t *fallback)
{
    lv_font_t *f = lv_tiny_ttf_create_data(font_ttf_start, font_ttf_end - font_ttf_start, px);
    if (!f) {
        ESP_LOGE(TAG, "tiny_ttf font %d px failed, using built-in", px);
        return (lv_font_t *)fallback;
    }
    f->fallback = fallback;  // LV_SYMBOL_* glyphs come from the built-in font
    return f;
}

static lv_obj_t *make_label(lv_obj_t *parent, const lv_font_t *font, uint32_t color,
                            const char *text)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(color), 0);
    lv_label_set_text(l, text);
    return l;
}

// Label with a fixed width that wraps (or is cut with "..." if dots) instead of overflowing.
static lv_obj_t *make_text(lv_obj_t *parent, const lv_font_t *font, uint32_t color,
                           const char *text, int width, bool dots)
{
    lv_obj_t *l = make_label(parent, font, color, text);
    lv_obj_set_width(l, width);
    lv_label_set_long_mode(l, dots ? LV_LABEL_LONG_DOT : LV_LABEL_LONG_WRAP);
    // LONG_DOT only truncates with a fixed height, otherwise the label still wraps.
    if (dots) lv_obj_set_height(l, lv_font_get_line_height(font));
    lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
    return l;
}

static lv_obj_t *make_button(lv_obj_t *parent, int w, int h, uint32_t color, const char *text,
                             const lv_font_t *font, lv_obj_t **out_label)
{
    lv_obj_t *b = lv_button_create(parent);
    lv_obj_set_size(b, w, h);
    lv_obj_set_style_radius(b, 14, 0);
    lv_obj_set_style_bg_color(b, lv_color_hex(color), 0);
    lv_obj_set_style_bg_color(b, lv_color_hex(0x263238), LV_STATE_DISABLED);
    lv_obj_set_style_shadow_width(b, 0, 0);
    lv_obj_t *l = make_text(b, font, COLOR_TEXT, text, w - 16, false);
    lv_obj_center(l);
    if (out_label) *out_label = l;
    return b;
}

static lv_obj_t *make_screen(void)
{
    lv_obj_t *scr = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(scr, lv_color_hex(COLOR_BG), 0);
    lv_obj_set_style_pad_top(scr, BAR_H, 0);
    lv_obj_remove_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
    return scr;
}

// --- events (LVGL task) -------------------------------------------------------------------

static void on_identify(lv_event_t *e)
{
    if (s_cb) s_cb(UI_EV_IDENTIFY);
}

static void on_open_train(lv_event_t *e)
{
    lv_screen_load_anim(s_train, LV_SCREEN_LOAD_ANIM_MOVE_LEFT, 150, 0, false);
}

static void on_back(lv_event_t *e)
{
    lv_screen_load_anim(s_home, LV_SCREEN_LOAD_ANIM_MOVE_RIGHT, 150, 0, false);
}

static void on_photo(lv_event_t *e)
{
    if (s_cb) s_cb(UI_EV_TRAIN_PHOTO);
}

static void toast_hide(lv_timer_t *t)
{
    lv_obj_add_flag(s_toast, LV_OBJ_FLAG_HIDDEN);
}

// --- construction -------------------------------------------------------------------------

static void build_status_bar(void)
{
    lv_obj_t *bar = lv_obj_create(lv_layer_top());
    lv_obj_remove_style_all(bar);
    lv_obj_set_size(bar, LV_PCT(100), BAR_H);
    lv_obj_set_style_bg_opa(bar, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(bar, lv_color_hex(COLOR_BAR), 0);
    lv_obj_set_style_pad_hor(bar, 8, 0);
    // Symbols only, right-aligned like on a phone: server, WLAN, battery.
    lv_obj_t *icons = lv_obj_create(bar);
    lv_obj_remove_style_all(icons);
    lv_obj_set_size(icons, LV_SIZE_CONTENT, BAR_H);
    lv_obj_set_flex_flow(icons, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(icons, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(icons, 12, 0);
    lv_obj_align(icons, LV_ALIGN_RIGHT_MID, 0, 0);
    s_icon_server = make_label(icons, &lv_font_montserrat_20, COLOR_MUTED, LV_SYMBOL_DRIVE);
    s_icon_wifi = make_label(icons, &lv_font_montserrat_20, COLOR_MUTED, LV_SYMBOL_WIFI);
    s_icon_battery = make_label(icons, &lv_font_montserrat_20, COLOR_MUTED, "");

    s_toast = lv_label_create(lv_layer_top());
    lv_obj_set_style_text_font(s_toast, s_font_s, 0);
    lv_obj_set_style_text_color(s_toast, lv_color_white(), 0);
    lv_obj_set_style_bg_opa(s_toast, LV_OPA_90, 0);
    lv_obj_set_style_radius(s_toast, 10, 0);
    lv_obj_set_style_pad_all(s_toast, 10, 0);
    lv_obj_set_width(s_toast, 220);
    lv_label_set_long_mode(s_toast, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_align(s_toast, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(s_toast, LV_ALIGN_BOTTOM_MID, 0, -10);
    lv_obj_add_flag(s_toast, LV_OBJ_FLAG_HIDDEN);
    s_toast_timer = lv_timer_create(toast_hide, TOAST_MS, NULL);
    lv_timer_set_auto_delete(s_toast_timer, false);  // reused: pauses after its single run
    lv_timer_pause(s_toast_timer);
}

static void build_home(void)
{
    s_home = make_screen();
    lv_obj_t *hint = make_text(s_home, s_font_s, COLOR_MUTED, "Gerät anvisieren", 224, true);
    lv_obj_align(hint, LV_ALIGN_TOP_MID, 0, 10);

    lv_obj_t *b = make_button(s_home, 216, 150, COLOR_ACCENT, LV_SYMBOL_EYE_OPEN "\nErkennen",
                              s_font_l, NULL);
    lv_obj_align(b, LV_ALIGN_TOP_MID, 0, 36);
    lv_obj_add_event_cb(b, on_identify, LV_EVENT_CLICKED, NULL);

    b = make_button(s_home, 216, 64, COLOR_SECONDARY, LV_SYMBOL_IMAGE "  Training", s_font_m,
                    NULL);
    lv_obj_align(b, LV_ALIGN_TOP_MID, 0, 198);
    lv_obj_add_event_cb(b, on_open_train, LV_EVENT_CLICKED, NULL);

    s_home_info = make_text(s_home, s_font_s, COLOR_MUTED, "", 224, true);
    lv_obj_align(s_home_info, LV_ALIGN_BOTTOM_MID, 0, -6);
}

static void build_train(void)
{
    s_train = make_screen();
    s_train_back = make_button(s_train, 110, 44, COLOR_SECONDARY, LV_SYMBOL_LEFT " Zurück",
                               s_font_s, NULL);
    lv_obj_align(s_train_back, LV_ALIGN_TOP_LEFT, 12, 8);
    lv_obj_add_event_cb(s_train_back, on_back, LV_EVENT_CLICKED, NULL);
    lv_obj_t *title = make_text(s_train, s_font_m, COLOR_TEXT, "Training", 100, true);
    lv_obj_set_style_text_align(title, LV_TEXT_ALIGN_RIGHT, 0);
    lv_obj_align(title, LV_ALIGN_TOP_RIGHT, -14, 18);

    s_train_btn = make_button(s_train, 216, 140, COLOR_TRAIN, LV_SYMBOL_IMAGE "\nFoto", s_font_l,
                              &s_train_btn_label);
    lv_obj_align(s_train_btn, LV_ALIGN_TOP_MID, 0, 62);
    lv_obj_add_event_cb(s_train_btn, on_photo, LV_EVENT_CLICKED, NULL);

    s_train_spinner = lv_spinner_create(s_train_btn);
    lv_obj_set_size(s_train_spinner, 36, 36);
    lv_obj_align(s_train_spinner, LV_ALIGN_TOP_MID, 0, 14);
    lv_obj_add_flag(s_train_spinner, LV_OBJ_FLAG_HIDDEN);

    s_train_count = make_text(s_train, s_font_m, COLOR_TEXT, "Gesendet: 0", 224, true);
    lv_obj_align(s_train_count, LV_ALIGN_TOP_MID, 0, 212);

    s_train_detail = make_text(s_train, s_font_s, COLOR_MUTED,
                               "Aus verschiedenen Winkeln und Abständen fotografieren.", 224,
                               false);
    lv_obj_align(s_train_detail, LV_ALIGN_TOP_MID, 0, 244);
}

void ui_init(ui_event_cb_t cb)
{
    s_cb = cb;
    if (!lvgl_port_lock(0)) return;
    s_font_s = make_font(16, &lv_font_montserrat_14);
    s_font_m = make_font(22, &lv_font_montserrat_20);
    s_font_l = make_font(30, &lv_font_montserrat_28);
    build_status_bar();
    build_home();
    build_train();
    lv_screen_load(s_home);
    lvgl_port_unlock();
    ESP_LOGI(TAG, "ready");
}

// --- public updates (any task) ------------------------------------------------------------

void ui_set_status(const ui_status_t *st)
{
    char info[64];
    uint32_t c_wifi = !st->wifi            ? COLOR_ERR
                      : st->rssi >= -70    ? COLOR_OK
                      : st->rssi >= -80    ? COLOR_WARN
                                           : COLOR_WEAK;
    uint32_t c_srv = !st->wifi                     ? COLOR_MUTED
                     : st->server == UI_SRV_OK     ? COLOR_OK
                     : st->server == UI_SRV_AUTH   ? COLOR_WEAK
                     : st->server == UI_SRV_UNKNOWN ? COLOR_MUTED
                                                   : COLOR_ERR;
    const char *bat = "";
    uint32_t c_bat = COLOR_MUTED;
    if (st->battery_v > 0) {
        bat = st->battery_pct > 80   ? LV_SYMBOL_BATTERY_FULL
              : st->battery_pct > 55 ? LV_SYMBOL_BATTERY_3
              : st->battery_pct > 30 ? LV_SYMBOL_BATTERY_2
              : st->battery_pct > 10 ? LV_SYMBOL_BATTERY_1
                                     : LV_SYMBOL_BATTERY_EMPTY;
        c_bat = st->battery_pct < 15 ? COLOR_ERR : COLOR_TEXT;
    }
    if (st->server == UI_SRV_OK) {
        snprintf(info, sizeof(info), "Index: %d Geräte, %d Bilder", st->entities, st->images);
    } else if (st->server == UI_SRV_AUTH) {
        snprintf(info, sizeof(info), "Server: API-Key falsch");
    } else if (st->wifi && st->server == UI_SRV_OFFLINE) {
        snprintf(info, sizeof(info), "Server nicht erreichbar");
    } else if (!st->wifi) {
        snprintf(info, sizeof(info), "Kein WLAN");
    } else {
        info[0] = '\0';
    }
    if (!lvgl_port_lock(0)) return;
    lv_obj_set_style_text_color(s_icon_server, lv_color_hex(c_srv), 0);
    lv_obj_set_style_text_color(s_icon_wifi, lv_color_hex(c_wifi), 0);
    lv_label_set_text(s_icon_battery, bat);
    lv_obj_set_style_text_color(s_icon_battery, lv_color_hex(c_bat), 0);
    lv_label_set_text(s_home_info, info);
    lvgl_port_unlock();
}

void ui_toast(const char *text, bool error)
{
    if (!lvgl_port_lock(0)) return;
    lv_label_set_text(s_toast, text);
    lv_obj_set_style_bg_color(s_toast, lv_color_hex(error ? COLOR_ERR : 0x455A64), 0);
    lv_obj_remove_flag(s_toast, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(s_toast);
    lv_timer_reset(s_toast_timer);
    lv_timer_set_repeat_count(s_toast_timer, 1);
    lv_timer_resume(s_toast_timer);
    lvgl_port_unlock();
}

void ui_train_busy(const char *text)
{
    if (!lvgl_port_lock(0)) return;
    lv_obj_add_state(s_train_btn, LV_STATE_DISABLED);
    lv_obj_add_state(s_train_back, LV_STATE_DISABLED);
    lv_obj_remove_flag(s_train_spinner, LV_OBJ_FLAG_HIDDEN);
    lv_label_set_text(s_train_btn_label, text);
    lv_obj_align(s_train_btn_label, LV_ALIGN_BOTTOM_MID, 0, -18);
    lvgl_port_unlock();
}

void ui_train_done(bool ok, int sent_count, const char *detail)
{
    if (!lvgl_port_lock(0)) return;
    lv_obj_remove_state(s_train_btn, LV_STATE_DISABLED);
    lv_obj_remove_state(s_train_back, LV_STATE_DISABLED);
    lv_obj_add_flag(s_train_spinner, LV_OBJ_FLAG_HIDDEN);
    lv_label_set_text(s_train_btn_label, LV_SYMBOL_IMAGE "\nFoto");
    lv_obj_center(s_train_btn_label);
    lv_label_set_text_fmt(s_train_count, "Gesendet: %d", sent_count);
    lv_label_set_text(s_train_detail, detail);
    lv_obj_set_style_text_color(s_train_detail, lv_color_hex(ok ? COLOR_MUTED : COLOR_ERR), 0);
    lvgl_port_unlock();
}
