#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "esp_err.h"

// Start WLAN in station mode with credentials from .env. Reconnects automatically.
esp_err_t wifi_start(void);

// Block until connected (or timeout). Returns true if connected.
bool wifi_wait_connected(int timeout_ms);

bool wifi_is_connected(void);

// Current IP as text ("-" if not connected).
void wifi_get_ip(char *buf, size_t len);

// RSSI in dBm, 0 if not connected.
int wifi_get_rssi(void);

// Modem sleep on/off (off by default: weak signal, latency). Used while the device is idle.
void wifi_set_power_save(bool on);
