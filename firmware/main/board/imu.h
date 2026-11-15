// QMI8658 IMU (I2C, shared bus with the touch controller). Not used by the app; it is only
// switched off to save power.
//
// Wake on Motion was tried as deep sleep wake-up source and does not work on this board:
// the IMU detects motion (STATUS1 WoM bit), but its INT1 never reaches GPIO3 (stays low for
// every CAL1_H/CTRL1/CTRL8 setting, not even the CTRL9 handshake pulse shows up), although
// the schematic connects IMU_INT1 to IO3. Measured 2026-11-15.
#pragma once

#include "esp_err.h"

// Find the IMU, leave a possibly active WoM mode and switch all sensors off.
esp_err_t imu_init(void);
