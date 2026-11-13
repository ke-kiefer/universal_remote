#pragma once

#include "esp_err.h"

esp_err_t battery_init(void);

// Battery voltage in volts (averaged), or a negative value on error.
float battery_voltage(void);

// Rough state of charge 0..100 % from the LiPo voltage (3.3 V .. 4.2 V, linear).
int battery_percent(float volts);
