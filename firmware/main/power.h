// Power management: ACTIVE -> DIM -> IDLE after touch inactivity (times from .env).
//   DIM:  backlight reduced
//   IDLE: backlight off, camera in software standby, optional WLAN modem sleep
// A touch while DIM/IDLE only wakes the device (the UI swallows it, see ui_set_wake_guard).
// All functions are called from the app task only.
#pragma once

typedef enum { POWER_ACTIVE, POWER_DIM, POWER_IDLE } power_state_t;

// Configure dynamic frequency scaling. Call once at startup.
void power_init(void);

// Check inactivity and step down if due. Call periodically.
void power_update(void);

// Back to ACTIVE now (wake touch).
void power_wake(void);

// Restart the inactivity timer (after app work such as a request finished).
void power_kick(void);

power_state_t power_state(void);
