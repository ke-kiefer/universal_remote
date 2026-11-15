// Power management (times from .env), driven by touch inactivity:
//   ACTIVE -> DIM:   backlight reduced (DIM_AFTER_S)
//   DIM    -> IDLE:  backlight off, camera standby, optional WLAN modem sleep (IDLE_AFTER_S)
//   IDLE   -> SLEEP: light sleep with WLAN and LCD off (SLEEP_AFTER_S); a touch or the BOOT
//                    key wakes up instantly (RAM, UI and camera AF firmware are kept)
// "Off" (menu, low battery) is deep sleep that only the BOOT key ends (restart).
// A touch while DIM/IDLE only wakes the device (the UI swallows it, see ui_set_wake_guard).
// All functions are called from the app task only.
//
// Wake on motion via the IMU does not work on this board (see board/imu.h).
#pragma once

#include <stdbool.h>

typedef enum { POWER_ACTIVE, POWER_DIM, POWER_IDLE } power_state_t;

// Log the wake-up cause, configure frequency scaling, switch the IMU off. Call once at
// startup after board_display_init (the IMU shares the touch I2C bus).
void power_init(void);

// Check inactivity and step down (up to light sleep) if due. Call periodically.
void power_update(void);

// Back to ACTIVE now (wake touch).
void power_wake(void);

// Restart the inactivity timer (after app work such as a request finished).
void power_kick(void);

power_state_t power_state(void);

// Switch off: deep sleep, only the BOOT key wakes (restart). Does not return.
void power_off(const char *reason);

// Battery voltage check (call with every new reading, v < 0 = unknown). Returns true after
// two consecutive readings below BATTERY_OFF_V while not connected to a PC.
bool power_check_battery(float v);

// True while a USB host (PC) is connected (not a charger or power bank).
bool power_usb_host_connected(void);
