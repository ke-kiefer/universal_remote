// QMI8658: only switched off. Registers from the QMI8658C datasheet (Rev 0.6, in the
// Waveshare demo package under Arduino/libraries/FastIMU/datasheets); the chip on this board
// behaves like a QMI8658A (CTRL9 CmdDone in STATUSINT bit 7). I2C address 0x6B as in the
// Waveshare demos.

#include "imu.h"

#include <stdbool.h>

#include "board.h"
#include "driver/i2c_master.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "imu";

#define IMU_ADDR 0x6B
#define IMU_ADDR_ALT 0x6A  // SA0 low

#define REG_WHO_AM_I 0x00  // 0x05
#define REG_CTRL7 0x08     // sensor enable
#define REG_CTRL9 0x0A     // command
#define REG_CAL1_L 0x0B    // WoM threshold (0 = WoM off)
#define REG_CAL1_H 0x0C
#define REG_STATUSINT 0x2D  // bit7 CmdDone (QMI8658A)
#define REG_STATUS1 0x2F    // bit0 CmdDone (QMI8658C), bit2 WoM
#define CTRL9_CMD_WOM 0x08  // CTRL_CMD_WRITE_WOM_SETTING
#define CTRL9_CMD_ACK 0x00

static i2c_master_dev_handle_t s_dev;

static esp_err_t wr(uint8_t reg, uint8_t val)
{
    const uint8_t buf[2] = {reg, val};
    return i2c_master_transmit(s_dev, buf, 2, 50);
}

static int rd(uint8_t reg)
{
    uint8_t v = 0;
    if (i2c_master_transmit_receive(s_dev, &reg, 1, &v, 1, 50) != ESP_OK) return -1;
    return v;
}

static bool cmd_done(void)
{
    int si = rd(REG_STATUSINT), s1 = rd(REG_STATUS1);
    return (si >= 0 && (si & 0x80)) || (s1 >= 0 && (s1 & 0x01));
}

static bool wait_done(bool want, int timeout_ms)
{
    int64_t end = esp_timer_get_time() + (int64_t)timeout_ms * 1000;
    bool d;
    while ((d = cmd_done()) != want && esp_timer_get_time() < end) {
        vTaskDelay(pdMS_TO_TICKS(2));
    }
    return d == want;
}

// CTRL9 handshake: CmdDone clear -> command -> CmdDone set -> ACK -> CmdDone clear.
static esp_err_t ctrl9(uint8_t cmd)
{
    if (!wait_done(false, 20)) {
        wr(REG_CTRL9, CTRL9_CMD_ACK);
        wait_done(false, 50);
    }
    wr(REG_CTRL9, cmd);
    bool done = wait_done(true, 100);
    wr(REG_CTRL9, CTRL9_CMD_ACK);
    bool cleared = wait_done(false, 50);
    return done && cleared ? ESP_OK : ESP_ERR_TIMEOUT;
}

esp_err_t imu_init(void)
{
    i2c_master_bus_handle_t bus = board_i2c_bus();
    if (!bus) return ESP_ERR_INVALID_STATE;
    const uint16_t addrs[] = {IMU_ADDR, IMU_ADDR_ALT};
    for (int i = 0; i < 2; i++) {
        if (i2c_master_probe(bus, addrs[i], 50) != ESP_OK) continue;
        const i2c_device_config_t cfg = {
            .dev_addr_length = I2C_ADDR_BIT_LEN_7,
            .device_address = addrs[i],
            .scl_speed_hz = 400000,
        };
        if (i2c_master_bus_add_device(bus, &cfg, &s_dev) != ESP_OK) return ESP_FAIL;
        // Sensors off, WoM off (threshold 0).
        wr(REG_CTRL7, 0x00);
        wr(REG_CAL1_L, 0x00);
        wr(REG_CAL1_H, 0x00);
        esp_err_t err = ctrl9(CTRL9_CMD_WOM);
        rd(REG_STATUS1);
        ESP_LOGI(TAG, "QMI8658 at 0x%02x (id 0x%02x) switched off (%s)", addrs[i],
                 rd(REG_WHO_AM_I), esp_err_to_name(err));
        return ESP_OK;
    }
    ESP_LOGW(TAG, "QMI8658 not found");
    return ESP_ERR_NOT_FOUND;
}
