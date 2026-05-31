/**
 * @file tsl2591.c
 * @brief TSL2591 Light Sensor Driver Implementation
 */

#include "tsl2591.h"
#include "esp_log.h"
#include <string.h>

static const char *TAG = "TSL2591";

/* Integration time in milliseconds for delay calculation */
static const uint16_t integration_time_ms[] = {
    100, 200, 300, 400, 500, 600
};

/* Gain multiplier values */
static const float gain_multiplier[] = {
    1.0F,       // LOW (1x)
    25.0F,      // MED (25x)
    428.0F,     // HIGH (428x)
    9876.0F     // MAX (9876x)
};

/**
 * @brief Write a byte to TSL2591 register
 */
static esp_err_t tsl2591_write_register(tsl2591_handle_t *handle, uint8_t reg, uint8_t data)
{
    uint8_t write_buf[2] = {TSL2591_COMMAND_BIT | reg, data};

    esp_err_t ret = i2c_master_write_to_device(
        handle->i2c_port,
        handle->dev_addr,
        write_buf,
        sizeof(write_buf),
        pdMS_TO_TICKS(1000)
    );

    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to write register 0x%02X: %s", reg, esp_err_to_name(ret));
    }

    return ret;
}

/**
 * @brief Read a byte from TSL2591 register
 */
static esp_err_t tsl2591_read_register(tsl2591_handle_t *handle, uint8_t reg, uint8_t *data)
{
    uint8_t cmd = TSL2591_COMMAND_BIT | reg;

    esp_err_t ret = i2c_master_write_read_device(
        handle->i2c_port,
        handle->dev_addr,
        &cmd,
        1,
        data,
        1,
        pdMS_TO_TICKS(1000)
    );

    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to read register 0x%02X: %s", reg, esp_err_to_name(ret));
    }

    return ret;
}

/**
 * @brief Read 16-bit word from TSL2591 register
 */
static esp_err_t tsl2591_read_word(tsl2591_handle_t *handle, uint8_t reg, uint16_t *data)
{
    uint8_t cmd = TSL2591_COMMAND_BIT | TSL2591_WORD_BIT | reg;
    uint8_t buffer[2];

    esp_err_t ret = i2c_master_write_read_device(
        handle->i2c_port,
        handle->dev_addr,
        &cmd,
        1,
        buffer,
        2,
        pdMS_TO_TICKS(1000)
    );

    if (ret == ESP_OK) {
        *data = (uint16_t)buffer[1] << 8 | buffer[0];
    } else {
        ESP_LOGE(TAG, "Failed to read word from register 0x%02X: %s", reg, esp_err_to_name(ret));
    }

    return ret;
}

esp_err_t tsl2591_init(const tsl2591_config_t *config, tsl2591_handle_t *handle)
{
    if (config == NULL || handle == NULL) {
        ESP_LOGE(TAG, "Invalid parameters");
        return ESP_ERR_INVALID_ARG;
    }

    ESP_LOGI(TAG, "Initializing TSL2591 sensor...");

    // Configure I2C
    i2c_config_t i2c_conf = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = config->sda_io_num,
        .scl_io_num = config->scl_io_num,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = config->clk_speed,
    };

    esp_err_t ret = i2c_param_config(config->i2c_port, &i2c_conf);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2C param config failed: %s", esp_err_to_name(ret));
        return ret;
    }

    ret = i2c_driver_install(config->i2c_port, I2C_MODE_MASTER, 0, 0, 0);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2C driver install failed: %s", esp_err_to_name(ret));
        return ret;
    }

    // Initialize handle
    handle->i2c_port = config->i2c_port;
    handle->dev_addr = TSL2591_ADDR;
    handle->integration = config->integration;
    handle->gain = config->gain;
    handle->initialized = false;

    // Check device ID
    uint8_t device_id;
    ret = tsl2591_get_device_id(handle, &device_id);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to read device ID");
        i2c_driver_delete(config->i2c_port);
        return ret;
    }

    if (device_id != TSL2591_DEVICE_ID) {
        ESP_LOGE(TAG, "Invalid device ID: 0x%02X (expected 0x%02X)", device_id, TSL2591_DEVICE_ID);
        i2c_driver_delete(config->i2c_port);
        return ESP_ERR_NOT_FOUND;
    }

    ESP_LOGI(TAG, "TSL2591 device ID verified: 0x%02X", device_id);

    // Enable sensor
    ret = tsl2591_enable(handle, true);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to enable sensor");
        i2c_driver_delete(config->i2c_port);
        return ret;
    }

    // Configure gain and integration time
    ret = tsl2591_set_gain(handle, config->gain);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to set gain");
        i2c_driver_delete(config->i2c_port);
        return ret;
    }

    ret = tsl2591_set_integration_time(handle, config->integration);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to set integration time");
        i2c_driver_delete(config->i2c_port);
        return ret;
    }

    handle->initialized = true;
    ESP_LOGI(TAG, "TSL2591 initialized successfully");

    return ESP_OK;
}

esp_err_t tsl2591_deinit(tsl2591_handle_t *handle)
{
    if (handle == NULL || !handle->initialized) {
        return ESP_ERR_INVALID_ARG;
    }

    // Disable sensor
    tsl2591_enable(handle, false);

    // Delete I2C driver
    esp_err_t ret = i2c_driver_delete(handle->i2c_port);
    if (ret == ESP_OK) {
        handle->initialized = false;
        ESP_LOGI(TAG, "TSL2591 deinitialized");
    }

    return ret;
}

esp_err_t tsl2591_enable(tsl2591_handle_t *handle, bool enable)
{
    if (handle == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t enable_val = enable ? (TSL2591_ENABLE_POWERON | TSL2591_ENABLE_AEN) : TSL2591_ENABLE_POWEROFF;

    esp_err_t ret = tsl2591_write_register(handle, TSL2591_REGISTER_ENABLE, enable_val);
    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "Sensor %s", enable ? "enabled" : "disabled");
    }

    return ret;
}

esp_err_t tsl2591_set_gain(tsl2591_handle_t *handle, tsl2591_gain_t gain)
{
    if (handle == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    // Read current config
    uint8_t config;
    esp_err_t ret = tsl2591_read_register(handle, TSL2591_REGISTER_CONFIG, &config);
    if (ret != ESP_OK) {
        return ret;
    }

    // Update gain bits
    config = (config & 0xCF) | gain;

    ret = tsl2591_write_register(handle, TSL2591_REGISTER_CONFIG, config);
    if (ret == ESP_OK) {
        handle->gain = gain;
        ESP_LOGI(TAG, "Gain set to 0x%02X", gain);
    }

    return ret;
}

esp_err_t tsl2591_set_integration_time(tsl2591_handle_t *handle, tsl2591_integration_time_t integration)
{
    if (handle == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    // Read current config
    uint8_t config;
    esp_err_t ret = tsl2591_read_register(handle, TSL2591_REGISTER_CONFIG, &config);
    if (ret != ESP_OK) {
        return ret;
    }

    // Update integration time bits
    config = (config & 0xF8) | integration;

    ret = tsl2591_write_register(handle, TSL2591_REGISTER_CONFIG, config);
    if (ret == ESP_OK) {
        handle->integration = integration;
        ESP_LOGI(TAG, "Integration time set to %d ms", integration_time_ms[integration]);
    }

    return ret;
}

esp_err_t tsl2591_get_device_id(tsl2591_handle_t *handle, uint8_t *id)
{
    if (handle == NULL || id == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    return tsl2591_read_register(handle, TSL2591_REGISTER_DEVICE_ID, id);
}

esp_err_t tsl2591_get_status(tsl2591_handle_t *handle, uint8_t *status)
{
    if (handle == NULL || status == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    return tsl2591_read_register(handle, TSL2591_REGISTER_DEVICE_STATUS, status);
}

esp_err_t tsl2591_read_raw_data(tsl2591_handle_t *handle, uint16_t *ch0, uint16_t *ch1)
{
    if (handle == NULL || !handle->initialized || ch0 == NULL || ch1 == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    // Wait for data to be ready (integration time + margin)
    vTaskDelay(pdMS_TO_TICKS(integration_time_ms[handle->integration] + 10));

    // Check if data is valid
    uint8_t status;
    esp_err_t ret = tsl2591_get_status(handle, &status);
    if (ret != ESP_OK) {
        return ret;
    }

    if (!(status & TSL2591_STATUS_AVALID)) {
        ESP_LOGW(TAG, "ALS data not valid yet");
        return ESP_ERR_INVALID_STATE;
    }

    // Read CH0 data
    ret = tsl2591_read_word(handle, TSL2591_REGISTER_C0DATAL, ch0);
    if (ret != ESP_OK) {
        return ret;
    }

    // Read CH1 data
    ret = tsl2591_read_word(handle, TSL2591_REGISTER_C1DATAL, ch1);
    if (ret != ESP_OK) {
        return ret;
    }

    ESP_LOGD(TAG, "Raw data - CH0: %u, CH1: %u", *ch0, *ch1);

    return ESP_OK;
}

esp_err_t tsl2591_calculate_lux(tsl2591_handle_t *handle, uint16_t ch0, uint16_t ch1, float *lux)
{
    if (handle == NULL || lux == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    // Check for saturation
    if (ch0 == TSL2591_MAX_COUNT || ch1 == TSL2591_MAX_COUNT) {
        ESP_LOGW(TAG, "Sensor saturated (CH0: %u, CH1: %u)", ch0, ch1);
        *lux = 0.0F;
        return ESP_ERR_INVALID_STATE;
    }

    // Check for zero readings
    if (ch0 == 0 && ch1 == 0) {
        *lux = 0.0F;
        return ESP_OK;
    }

    // Get integration time and gain multipliers
    float atime = (float)integration_time_ms[handle->integration];
    float again = gain_multiplier[handle->gain >> 4];  // Gain is in bits 4-5

    // Calculate CPL (counts per lux)
    float cpl = (atime * again) / TSL2591_LUX_DF;

    // Calculate lux
    float lux1 = ((float)ch0 - (TSL2591_LUX_COEFB * (float)ch1)) / cpl;
    float lux2 = ((TSL2591_LUX_COEFC * (float)ch0) - (TSL2591_LUX_COEFD * (float)ch1)) / cpl;

    // Use the maximum of the two calculations
    *lux = (lux1 > lux2) ? lux1 : lux2;

    // Ensure non-negative
    if (*lux < 0.0F) {
        *lux = 0.0F;
    }

    ESP_LOGD(TAG, "Calculated lux: %.2f", *lux);

    return ESP_OK;
}

esp_err_t tsl2591_get_full_luminosity(tsl2591_handle_t *handle, uint32_t *luminosity)
{
    if (handle == NULL || luminosity == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    uint16_t ch0, ch1;
    esp_err_t ret = tsl2591_read_raw_data(handle, &ch0, &ch1);
    if (ret == ESP_OK) {
        *luminosity = ((uint32_t)ch1 << 16) | ch0;
    }

    return ret;
}

esp_err_t tsl2591_get_lux(tsl2591_handle_t *handle, float *lux)
{
    if (handle == NULL || lux == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    uint16_t ch0, ch1;
    esp_err_t ret = tsl2591_read_raw_data(handle, &ch0, &ch1);
    if (ret != ESP_OK) {
        return ret;
    }

    return tsl2591_calculate_lux(handle, ch0, ch1, lux);
}

esp_err_t tsl2591_set_interrupt_threshold(tsl2591_handle_t *handle, uint16_t lower_threshold, uint16_t upper_threshold)
{
    if (handle == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    // Write lower threshold
    esp_err_t ret = tsl2591_write_register(handle, TSL2591_REGISTER_AILTL, lower_threshold & 0xFF);
    if (ret != ESP_OK) return ret;

    ret = tsl2591_write_register(handle, TSL2591_REGISTER_AILTH, (lower_threshold >> 8) & 0xFF);
    if (ret != ESP_OK) return ret;

    // Write upper threshold
    ret = tsl2591_write_register(handle, TSL2591_REGISTER_AIHTL, upper_threshold & 0xFF);
    if (ret != ESP_OK) return ret;

    ret = tsl2591_write_register(handle, TSL2591_REGISTER_AIHTH, (upper_threshold >> 8) & 0xFF);
    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "Interrupt thresholds set: %u - %u", lower_threshold, upper_threshold);
    }

    return ret;
}

esp_err_t tsl2591_enable_interrupt(tsl2591_handle_t *handle, bool enable, tsl2591_persist_t persist)
{
    if (handle == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    // Set persistence filter
    esp_err_t ret = tsl2591_write_register(handle, TSL2591_REGISTER_PERSIST, persist);
    if (ret != ESP_OK) return ret;

    // Read current enable register
    uint8_t enable_val;
    ret = tsl2591_read_register(handle, TSL2591_REGISTER_ENABLE, &enable_val);
    if (ret != ESP_OK) return ret;

    // Update interrupt enable bit
    if (enable) {
        enable_val |= TSL2591_ENABLE_AIEN;
    } else {
        enable_val &= ~TSL2591_ENABLE_AIEN;
    }

    ret = tsl2591_write_register(handle, TSL2591_REGISTER_ENABLE, enable_val);
    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "Interrupt %s (persist: %d)", enable ? "enabled" : "disabled", persist);
    }

    return ret;
}

esp_err_t tsl2591_clear_interrupt(tsl2591_handle_t *handle)
{
    if (handle == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t cmd = TSL2591_CLEAR_INT;

    esp_err_t ret = i2c_master_write_to_device(
        handle->i2c_port,
        handle->dev_addr,
        &cmd,
        1,
        pdMS_TO_TICKS(1000)
    );

    if (ret == ESP_OK) {
        ESP_LOGD(TAG, "Interrupt cleared");
    } else {
        ESP_LOGE(TAG, "Failed to clear interrupt: %s", esp_err_to_name(ret));
    }

    return ret;
}
