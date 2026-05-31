/**
 * @file tsl2591.h
 * @brief TSL2591 Light Sensor Driver for ESP32-S3
 *
 * This driver provides functions to communicate with TSL25911FN sensor
 * via I2C interface.
 *
 * @author ESP32-S3 TSL2591 Project
 * @date 2025-11-04
 */

#ifndef TSL2591_H
#define TSL2591_H

#include <stdint.h>
#include <stdbool.h>
#include "driver/i2c.h"
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* TSL2591 I2C Address */
#define TSL2591_ADDR                    0x29

/* TSL2591 Commands */
#define TSL2591_COMMAND_BIT             0xA0    // Command register (1010 0000)
#define TSL2591_CLEAR_INT               0xE7    // Clear interrupt
#define TSL2591_TEST_INT                0xE4    // Test interrupt
#define TSL2591_WORD_BIT                0x20    // Read/write word (2 bytes)

/* TSL2591 Registers */
#define TSL2591_REGISTER_ENABLE         0x00    // Enable register
#define TSL2591_REGISTER_CONFIG         0x01    // Configuration register
#define TSL2591_REGISTER_AILTL          0x04    // ALS interrupt low threshold low byte
#define TSL2591_REGISTER_AILTH          0x05    // ALS interrupt low threshold high byte
#define TSL2591_REGISTER_AIHTL          0x06    // ALS interrupt high threshold low byte
#define TSL2591_REGISTER_AIHTH          0x07    // ALS interrupt high threshold high byte
#define TSL2591_REGISTER_NPAILTL        0x08    // No persist ALS interrupt low threshold low byte
#define TSL2591_REGISTER_NPAILTH        0x09    // No persist ALS interrupt low threshold high byte
#define TSL2591_REGISTER_NPAIHTL        0x0A    // No persist ALS interrupt high threshold low byte
#define TSL2591_REGISTER_NPAIHTH        0x0B    // No persist ALS interrupt high threshold high byte
#define TSL2591_REGISTER_PERSIST        0x0C    // Interrupt persistence filter
#define TSL2591_REGISTER_DEVICE_ID      0x12    // Device ID (should be 0x50)
#define TSL2591_REGISTER_DEVICE_STATUS  0x13    // Device status
#define TSL2591_REGISTER_C0DATAL        0x14    // CH0 ADC data low byte
#define TSL2591_REGISTER_C0DATAH        0x15    // CH0 ADC data high byte
#define TSL2591_REGISTER_C1DATAL        0x16    // CH1 ADC data low byte
#define TSL2591_REGISTER_C1DATAH        0x17    // CH1 ADC data high byte

/* Enable Register Bits */
#define TSL2591_ENABLE_POWEROFF         0x00
#define TSL2591_ENABLE_POWERON          0x01
#define TSL2591_ENABLE_AEN              0x02    // ALS Enable
#define TSL2591_ENABLE_AIEN             0x10    // ALS Interrupt Enable
#define TSL2591_ENABLE_SAI              0x40    // Sleep After Interrupt
#define TSL2591_ENABLE_NPIEN            0x80    // No Persist Interrupt Enable

/* Device ID */
#define TSL2591_DEVICE_ID               0x50

/* Status Register Bits */
#define TSL2591_STATUS_AVALID           0x01    // ALS Valid
#define TSL2591_STATUS_AINT             0x10    // ALS Interrupt

/* LUX Coefficients */
#define TSL2591_LUX_DF                  408.0F  // Device factor
#define TSL2591_LUX_COEFB               1.64F   // CH0 coefficient
#define TSL2591_LUX_COEFC               0.59F   // CH1 coefficient A
#define TSL2591_LUX_COEFD               0.86F   // CH2 coefficient B

/* Maximum ADC Value */
#define TSL2591_MAX_COUNT               0xFFFF  // Maximum count (16-bit ADC)

/**
 * @brief TSL2591 integration time options
 */
typedef enum {
    TSL2591_INTEGRATIONTIME_100MS = 0x00,   // 100 ms
    TSL2591_INTEGRATIONTIME_200MS = 0x01,   // 200 ms
    TSL2591_INTEGRATIONTIME_300MS = 0x02,   // 300 ms
    TSL2591_INTEGRATIONTIME_400MS = 0x03,   // 400 ms
    TSL2591_INTEGRATIONTIME_500MS = 0x04,   // 500 ms
    TSL2591_INTEGRATIONTIME_600MS = 0x05,   // 600 ms
} tsl2591_integration_time_t;

/**
 * @brief TSL2591 gain options
 */
typedef enum {
    TSL2591_GAIN_LOW  = 0x00,   // Low gain (1x)
    TSL2591_GAIN_MED  = 0x10,   // Medium gain (25x)
    TSL2591_GAIN_HIGH = 0x20,   // High gain (428x)
    TSL2591_GAIN_MAX  = 0x30,   // Max gain (9876x)
} tsl2591_gain_t;

/**
 * @brief TSL2591 interrupt persistence
 */
typedef enum {
    TSL2591_PERSIST_EVERY = 0x00,   // Every ALS cycle generates an interrupt
    TSL2591_PERSIST_ANY   = 0x01,   // Any value outside threshold
    TSL2591_PERSIST_2     = 0x02,   // 2 integration times out of threshold
    TSL2591_PERSIST_3     = 0x03,   // 3 integration times out of threshold
    TSL2591_PERSIST_5     = 0x04,   // 5 integration times out of threshold
    TSL2591_PERSIST_10    = 0x05,   // 10 integration times out of threshold
    TSL2591_PERSIST_15    = 0x06,   // 15 integration times out of threshold
    TSL2591_PERSIST_20    = 0x07,   // 20 integration times out of threshold
    TSL2591_PERSIST_25    = 0x08,   // 25 integration times out of threshold
    TSL2591_PERSIST_30    = 0x09,   // 30 integration times out of threshold
    TSL2591_PERSIST_35    = 0x0A,   // 35 integration times out of threshold
    TSL2591_PERSIST_40    = 0x0B,   // 40 integration times out of threshold
    TSL2591_PERSIST_45    = 0x0C,   // 45 integration times out of threshold
    TSL2591_PERSIST_50    = 0x0D,   // 50 integration times out of threshold
    TSL2591_PERSIST_55    = 0x0E,   // 55 integration times out of threshold
    TSL2591_PERSIST_60    = 0x0F,   // 60 integration times out of threshold
} tsl2591_persist_t;

/**
 * @brief TSL2591 device handle structure
 */
typedef struct {
    i2c_port_t i2c_port;                        // I2C port number
    uint8_t dev_addr;                           // I2C device address
    tsl2591_integration_time_t integration;     // Integration time
    tsl2591_gain_t gain;                        // Gain setting
    bool initialized;                           // Initialization flag
} tsl2591_handle_t;

/**
 * @brief TSL2591 configuration structure
 */
typedef struct {
    i2c_port_t i2c_port;                        // I2C port number (I2C_NUM_0 or I2C_NUM_1)
    gpio_num_t sda_io_num;                      // GPIO number for SDA
    gpio_num_t scl_io_num;                      // GPIO number for SCL
    uint32_t clk_speed;                         // I2C clock speed (100000 or 400000)
    tsl2591_integration_time_t integration;     // Integration time
    tsl2591_gain_t gain;                        // Gain setting
} tsl2591_config_t;

/**
 * @brief Initialize TSL2591 sensor
 *
 * @param config Pointer to configuration structure
 * @param handle Pointer to device handle (will be initialized)
 * @return ESP_OK on success
 */
esp_err_t tsl2591_init(const tsl2591_config_t *config, tsl2591_handle_t *handle);

/**
 * @brief Deinitialize TSL2591 sensor and I2C driver
 *
 * @param handle Pointer to device handle
 * @return ESP_OK on success
 */
esp_err_t tsl2591_deinit(tsl2591_handle_t *handle);

/**
 * @brief Enable or disable TSL2591 sensor
 *
 * @param handle Pointer to device handle
 * @param enable true to enable, false to disable
 * @return ESP_OK on success
 */
esp_err_t tsl2591_enable(tsl2591_handle_t *handle, bool enable);

/**
 * @brief Set TSL2591 gain
 *
 * @param handle Pointer to device handle
 * @param gain Gain setting
 * @return ESP_OK on success
 */
esp_err_t tsl2591_set_gain(tsl2591_handle_t *handle, tsl2591_gain_t gain);

/**
 * @brief Set TSL2591 integration time
 *
 * @param handle Pointer to device handle
 * @param integration Integration time setting
 * @return ESP_OK on success
 */
esp_err_t tsl2591_set_integration_time(tsl2591_handle_t *handle, tsl2591_integration_time_t integration);

/**
 * @brief Get TSL2591 device ID
 *
 * @param handle Pointer to device handle
 * @param id Pointer to store device ID (should be 0x50)
 * @return ESP_OK on success
 */
esp_err_t tsl2591_get_device_id(tsl2591_handle_t *handle, uint8_t *id);

/**
 * @brief Get TSL2591 device status
 *
 * @param handle Pointer to device handle
 * @param status Pointer to store status byte
 * @return ESP_OK on success
 */
esp_err_t tsl2591_get_status(tsl2591_handle_t *handle, uint8_t *status);

/**
 * @brief Read raw ADC data from both channels
 *
 * @param handle Pointer to device handle
 * @param ch0 Pointer to store CH0 data (Visible + IR)
 * @param ch1 Pointer to store CH1 data (IR only)
 * @return ESP_OK on success
 */
esp_err_t tsl2591_read_raw_data(tsl2591_handle_t *handle, uint16_t *ch0, uint16_t *ch1);

/**
 * @brief Calculate lux value from raw ADC data
 *
 * @param handle Pointer to device handle
 * @param ch0 CH0 ADC data (Visible + IR)
 * @param ch1 CH1 ADC data (IR only)
 * @param lux Pointer to store calculated lux value
 * @return ESP_OK on success, ESP_ERR_INVALID_STATE if saturated
 */
esp_err_t tsl2591_calculate_lux(tsl2591_handle_t *handle, uint16_t ch0, uint16_t ch1, float *lux);

/**
 * @brief Read full luminosity (CH0 and CH1 combined)
 *
 * @param handle Pointer to device handle
 * @param luminosity Pointer to store 32-bit luminosity (CH1 << 16 | CH0)
 * @return ESP_OK on success
 */
esp_err_t tsl2591_get_full_luminosity(tsl2591_handle_t *handle, uint32_t *luminosity);

/**
 * @brief Get calculated lux value (convenience function)
 *
 * @param handle Pointer to device handle
 * @param lux Pointer to store lux value
 * @return ESP_OK on success, ESP_ERR_INVALID_STATE if saturated
 */
esp_err_t tsl2591_get_lux(tsl2591_handle_t *handle, float *lux);

/**
 * @brief Configure interrupt thresholds
 *
 * @param handle Pointer to device handle
 * @param lower_threshold Lower threshold value
 * @param upper_threshold Upper threshold value
 * @return ESP_OK on success
 */
esp_err_t tsl2591_set_interrupt_threshold(tsl2591_handle_t *handle, uint16_t lower_threshold, uint16_t upper_threshold);

/**
 * @brief Enable or disable interrupts
 *
 * @param handle Pointer to device handle
 * @param enable true to enable, false to disable
 * @param persist Persistence filter setting
 * @return ESP_OK on success
 */
esp_err_t tsl2591_enable_interrupt(tsl2591_handle_t *handle, bool enable, tsl2591_persist_t persist);

/**
 * @brief Clear interrupt status
 *
 * @param handle Pointer to device handle
 * @return ESP_OK on success
 */
esp_err_t tsl2591_clear_interrupt(tsl2591_handle_t *handle);

#ifdef __cplusplus
}
#endif

#endif // TSL2591_H
