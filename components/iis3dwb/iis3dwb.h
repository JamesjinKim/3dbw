/**
 * @file iis3dwb.h
 * @brief IIS3DWB 3-axis Digital Vibration Sensor Driver for ESP32
 *
 * This driver provides SPI communication with the IIS3DWB sensor
 * for high-bandwidth vibration monitoring applications.
 *
 * Key Features:
 * - 3-axis accelerometer with ultra-wide bandwidth (DC to 6 kHz)
 * - Fixed 26.667 kHz output data rate
 * - Full-scale: ±2g/±4g/±8g/±16g selectable
 * - 3KB embedded FIFO
 */

#ifndef IIS3DWB_H
#define IIS3DWB_H

#include "esp_err.h"
#include "driver/spi_master.h"
#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ============================================================================
 * Device Identification
 * ============================================================================ */
#define IIS3DWB_DEVICE_ID           0x7B    /**< WHO_AM_I register value */
#define IIS3DWB_I2C_ADDR            0x6A    /**< Default I2C address (SA0=0) */
#define IIS3DWB_I2C_ADDR_ALT        0x6B    /**< Alternate I2C address (SA0=1) */

/* ============================================================================
 * Register Addresses
 * ============================================================================ */
/* Identification Registers */
#define IIS3DWB_REG_PIN_CTRL        0x02    /**< Pull-up control on SDO/SA0 pin */
#define IIS3DWB_REG_FIFO_CTRL1      0x07    /**< FIFO watermark threshold [7:0] */
#define IIS3DWB_REG_FIFO_CTRL2      0x08    /**< FIFO watermark threshold [8] */
#define IIS3DWB_REG_FIFO_CTRL3      0x09    /**< FIFO batch data rate */
#define IIS3DWB_REG_FIFO_CTRL4      0x0A    /**< FIFO mode selection */
#define IIS3DWB_REG_COUNTER_BDR_REG1 0x0B   /**< Counter BDR register 1 */
#define IIS3DWB_REG_COUNTER_BDR_REG2 0x0C   /**< Counter BDR register 2 */
#define IIS3DWB_REG_INT1_CTRL       0x0D    /**< INT1 pin control */
#define IIS3DWB_REG_INT2_CTRL       0x0E    /**< INT2 pin control */
#define IIS3DWB_REG_WHO_AM_I        0x0F    /**< Device identification */
#define IIS3DWB_REG_CTRL1_XL        0x10    /**< Accelerometer control register 1 */
#define IIS3DWB_REG_CTRL3_C         0x12    /**< Control register 3 */
#define IIS3DWB_REG_CTRL4_C         0x13    /**< Control register 4 */
#define IIS3DWB_REG_CTRL5_C         0x14    /**< Control register 5 */
#define IIS3DWB_REG_CTRL6_C         0x15    /**< Control register 6 */
#define IIS3DWB_REG_CTRL7_C         0x16    /**< Control register 7 */
#define IIS3DWB_REG_CTRL8_XL        0x17    /**< Accelerometer control register 8 */
#define IIS3DWB_REG_CTRL10_C        0x19    /**< Control register 10 */
#define IIS3DWB_REG_ALL_INT_SRC     0x1A    /**< All interrupts source */
#define IIS3DWB_REG_WAKE_UP_SRC     0x1B    /**< Wake-up interrupt source */
#define IIS3DWB_REG_STATUS_REG      0x1E    /**< Status register */
#define IIS3DWB_REG_OUT_TEMP_L      0x20    /**< Temperature output low byte */
#define IIS3DWB_REG_OUT_TEMP_H      0x21    /**< Temperature output high byte */
#define IIS3DWB_REG_OUTX_L_A        0x28    /**< X-axis acceleration low byte */
#define IIS3DWB_REG_OUTX_H_A        0x29    /**< X-axis acceleration high byte */
#define IIS3DWB_REG_OUTY_L_A        0x2A    /**< Y-axis acceleration low byte */
#define IIS3DWB_REG_OUTY_H_A        0x2B    /**< Y-axis acceleration high byte */
#define IIS3DWB_REG_OUTZ_L_A        0x2C    /**< Z-axis acceleration low byte */
#define IIS3DWB_REG_OUTZ_H_A        0x2D    /**< Z-axis acceleration high byte */
#define IIS3DWB_REG_FIFO_STATUS1    0x3A    /**< FIFO status register 1 */
#define IIS3DWB_REG_FIFO_STATUS2    0x3B    /**< FIFO status register 2 */
#define IIS3DWB_REG_TIMESTAMP0      0x40    /**< Timestamp register 0 */
#define IIS3DWB_REG_TIMESTAMP1      0x41    /**< Timestamp register 1 */
#define IIS3DWB_REG_TIMESTAMP2      0x42    /**< Timestamp register 2 */
#define IIS3DWB_REG_TIMESTAMP3      0x43    /**< Timestamp register 3 */
#define IIS3DWB_REG_SLOPE_EN        0x56    /**< Slope filter enable */
#define IIS3DWB_REG_INTERRUPTS_EN   0x58    /**< Interrupts enable */
#define IIS3DWB_REG_WAKE_UP_THS     0x5B    /**< Wake-up threshold */
#define IIS3DWB_REG_WAKE_UP_DUR     0x5C    /**< Wake-up duration */
#define IIS3DWB_REG_MD1_CFG         0x5E    /**< INT1 routing configuration */
#define IIS3DWB_REG_MD2_CFG         0x5F    /**< INT2 routing configuration */
#define IIS3DWB_REG_INTERNAL_FREQ   0x63    /**< Internal frequency fine tuning */
#define IIS3DWB_REG_X_OFS_USR       0x73    /**< X-axis user offset */
#define IIS3DWB_REG_Y_OFS_USR       0x74    /**< Y-axis user offset */
#define IIS3DWB_REG_Z_OFS_USR       0x75    /**< Z-axis user offset */
#define IIS3DWB_REG_FIFO_DATA_OUT_TAG 0x78  /**< FIFO tag register */
#define IIS3DWB_REG_FIFO_DATA_OUT_X_L 0x79  /**< FIFO data output X low */
#define IIS3DWB_REG_FIFO_DATA_OUT_X_H 0x7A  /**< FIFO data output X high */
#define IIS3DWB_REG_FIFO_DATA_OUT_Y_L 0x7B  /**< FIFO data output Y low */
#define IIS3DWB_REG_FIFO_DATA_OUT_Y_H 0x7C  /**< FIFO data output Y high */
#define IIS3DWB_REG_FIFO_DATA_OUT_Z_L 0x7D  /**< FIFO data output Z low */
#define IIS3DWB_REG_FIFO_DATA_OUT_Z_H 0x7E  /**< FIFO data output Z high */

/* ============================================================================
 * Register Bit Definitions
 * ============================================================================ */
/* CTRL1_XL (0x10) - Accelerometer control register 1 */
#define IIS3DWB_CTRL1_XL_XL_EN      (1 << 7) /**< Accelerometer enable */
#define IIS3DWB_CTRL1_XL_LPF2_XL_EN (1 << 1) /**< LPF2 enable */

/* Full-scale selection (bits 3:2 of CTRL1_XL) */
typedef enum {
    IIS3DWB_FS_2G  = 0x00,  /**< ±2g full-scale */
    IIS3DWB_FS_4G  = 0x08,  /**< ±4g full-scale */
    IIS3DWB_FS_8G  = 0x0C,  /**< ±8g full-scale */
    IIS3DWB_FS_16G = 0x04,  /**< ±16g full-scale */
} iis3dwb_fs_xl_t;

/* CTRL3_C (0x12) - Control register 3 */
#define IIS3DWB_CTRL3_C_BOOT        (1 << 7) /**< Reboot memory content */
#define IIS3DWB_CTRL3_C_BDU         (1 << 6) /**< Block data update */
#define IIS3DWB_CTRL3_C_IF_INC      (1 << 2) /**< Auto-increment address */
#define IIS3DWB_CTRL3_C_SIM         (1 << 0) /**< SPI serial interface mode */
#define IIS3DWB_CTRL3_C_SW_RESET    (1 << 0) /**< Software reset */

/* CTRL6_C (0x15) - Control register 6 */
/* Filter bandwidth selection (bits 2:0) */
typedef enum {
    IIS3DWB_BW_ODR_DIV_4   = 0x00, /**< ODR/4 bandwidth */
    IIS3DWB_BW_ODR_DIV_10  = 0x01, /**< ODR/10 bandwidth */
    IIS3DWB_BW_ODR_DIV_20  = 0x02, /**< ODR/20 bandwidth */
    IIS3DWB_BW_ODR_DIV_45  = 0x03, /**< ODR/45 bandwidth */
    IIS3DWB_BW_ODR_DIV_100 = 0x04, /**< ODR/100 bandwidth */
    IIS3DWB_BW_ODR_DIV_200 = 0x05, /**< ODR/200 bandwidth */
    IIS3DWB_BW_ODR_DIV_400 = 0x06, /**< ODR/400 bandwidth */
    IIS3DWB_BW_ODR_DIV_800 = 0x07, /**< ODR/800 bandwidth */
} iis3dwb_bw_xl_t;

/* STATUS_REG (0x1E) */
#define IIS3DWB_STATUS_XLDA         (1 << 0) /**< Accelerometer data available */
#define IIS3DWB_STATUS_TDA          (1 << 2) /**< Temperature data available */

/* FIFO_CTRL4 (0x0A) - FIFO mode selection (bits 2:0) */
typedef enum {
    IIS3DWB_FIFO_BYPASS         = 0x00, /**< FIFO disabled */
    IIS3DWB_FIFO_MODE           = 0x01, /**< FIFO mode: stops collecting when full */
    IIS3DWB_FIFO_CONTINUOUS_WTM = 0x03, /**< Continuous to FIFO mode */
    IIS3DWB_FIFO_CONTINUOUS     = 0x06, /**< Continuous mode */
    IIS3DWB_FIFO_BYPASS_TO_CONT = 0x07, /**< Bypass to Continuous mode */
} iis3dwb_fifo_mode_t;

/* ============================================================================
 * Sensitivity Values (mg/LSB)
 * ============================================================================ */
#define IIS3DWB_SENSITIVITY_2G   0.061f  /**< Sensitivity for ±2g (mg/LSB) */
#define IIS3DWB_SENSITIVITY_4G   0.122f  /**< Sensitivity for ±4g (mg/LSB) */
#define IIS3DWB_SENSITIVITY_8G   0.244f  /**< Sensitivity for ±8g (mg/LSB) */
#define IIS3DWB_SENSITIVITY_16G  0.488f  /**< Sensitivity for ±16g (mg/LSB) */

/* Temperature sensitivity */
#define IIS3DWB_TEMP_SENSITIVITY 256.0f  /**< Temperature sensitivity (LSB/°C) */
#define IIS3DWB_TEMP_OFFSET      25.0f   /**< Temperature offset (°C at 0 LSB) */

/* ============================================================================
 * Configuration Structures
 * ============================================================================ */

/**
 * @brief IIS3DWB SPI configuration
 */
typedef struct {
    spi_host_device_t spi_host;     /**< SPI host (SPI2_HOST or SPI3_HOST) */
    int mosi_io_num;                /**< GPIO for MOSI (Master Out Slave In) */
    int miso_io_num;                /**< GPIO for MISO (Master In Slave Out) */
    int sclk_io_num;                /**< GPIO for SCLK (Serial Clock) */
    int cs_io_num;                  /**< GPIO for CS (Chip Select) */
    int clk_speed_hz;               /**< SPI clock speed (max 10 MHz) */
    iis3dwb_fs_xl_t full_scale;     /**< Full-scale selection */
    iis3dwb_bw_xl_t bandwidth;      /**< Low-pass filter bandwidth */
} iis3dwb_config_t;

/**
 * @brief IIS3DWB device handle
 */
typedef struct {
    spi_device_handle_t spi_handle; /**< SPI device handle */
    iis3dwb_fs_xl_t full_scale;     /**< Current full-scale setting */
    float sensitivity;              /**< Current sensitivity (mg/LSB) */
    bool initialized;               /**< Initialization status */
} iis3dwb_handle_t;

/**
 * @brief 3-axis acceleration data (raw 16-bit values)
 */
typedef struct {
    int16_t x;  /**< X-axis raw acceleration */
    int16_t y;  /**< Y-axis raw acceleration */
    int16_t z;  /**< Z-axis raw acceleration */
} iis3dwb_raw_data_t;

/**
 * @brief 3-axis acceleration data (in mg)
 */
typedef struct {
    float x_mg;  /**< X-axis acceleration in mg */
    float y_mg;  /**< Y-axis acceleration in mg */
    float z_mg;  /**< Z-axis acceleration in mg */
} iis3dwb_accel_data_t;

/* ============================================================================
 * API Functions
 * ============================================================================ */

/**
 * @brief Initialize IIS3DWB sensor
 *
 * This function initializes the SPI bus, configures the sensor,
 * and verifies device identification.
 *
 * @param config Pointer to configuration structure
 * @param handle Pointer to handle structure to be initialized
 * @return
 *     - ESP_OK: Success
 *     - ESP_ERR_INVALID_ARG: Invalid configuration
 *     - ESP_ERR_NOT_FOUND: Device not found (WHO_AM_I mismatch)
 *     - ESP_FAIL: Communication error
 */
esp_err_t iis3dwb_init(const iis3dwb_config_t *config, iis3dwb_handle_t *handle);

/**
 * @brief Deinitialize IIS3DWB sensor
 *
 * @param handle Pointer to device handle
 * @return
 *     - ESP_OK: Success
 *     - ESP_ERR_INVALID_ARG: Invalid handle
 */
esp_err_t iis3dwb_deinit(iis3dwb_handle_t *handle);

/**
 * @brief Read device ID (WHO_AM_I register)
 *
 * @param handle Pointer to device handle
 * @param device_id Pointer to store device ID
 * @return
 *     - ESP_OK: Success
 *     - ESP_FAIL: Communication error
 */
esp_err_t iis3dwb_get_device_id(iis3dwb_handle_t *handle, uint8_t *device_id);

/**
 * @brief Enable or disable the accelerometer
 *
 * @param handle Pointer to device handle
 * @param enable true to enable, false to disable (power-down)
 * @return
 *     - ESP_OK: Success
 *     - ESP_FAIL: Communication error
 */
esp_err_t iis3dwb_enable(iis3dwb_handle_t *handle, bool enable);

/**
 * @brief Set full-scale selection
 *
 * @param handle Pointer to device handle
 * @param fs Full-scale value (±2g, ±4g, ±8g, ±16g)
 * @return
 *     - ESP_OK: Success
 *     - ESP_FAIL: Communication error
 */
esp_err_t iis3dwb_set_full_scale(iis3dwb_handle_t *handle, iis3dwb_fs_xl_t fs);

/**
 * @brief Set low-pass filter bandwidth
 *
 * @param handle Pointer to device handle
 * @param bw Bandwidth value
 * @return
 *     - ESP_OK: Success
 *     - ESP_FAIL: Communication error
 */
esp_err_t iis3dwb_set_bandwidth(iis3dwb_handle_t *handle, iis3dwb_bw_xl_t bw);

/**
 * @brief Check if new acceleration data is available
 *
 * @param handle Pointer to device handle
 * @param available Pointer to store availability status
 * @return
 *     - ESP_OK: Success
 *     - ESP_FAIL: Communication error
 */
esp_err_t iis3dwb_data_ready(iis3dwb_handle_t *handle, bool *available);

/**
 * @brief Read raw acceleration data (3-axis)
 *
 * @param handle Pointer to device handle
 * @param raw_data Pointer to store raw data
 * @return
 *     - ESP_OK: Success
 *     - ESP_FAIL: Communication error
 */
esp_err_t iis3dwb_read_raw_data(iis3dwb_handle_t *handle, iis3dwb_raw_data_t *raw_data);

/**
 * @brief Read acceleration data in mg (3-axis)
 *
 * @param handle Pointer to device handle
 * @param accel_data Pointer to store acceleration data in mg
 * @return
 *     - ESP_OK: Success
 *     - ESP_FAIL: Communication error
 */
esp_err_t iis3dwb_read_accel_data(iis3dwb_handle_t *handle, iis3dwb_accel_data_t *accel_data);

/**
 * @brief Read temperature
 *
 * @param handle Pointer to device handle
 * @param temperature Pointer to store temperature in °C
 * @return
 *     - ESP_OK: Success
 *     - ESP_FAIL: Communication error
 */
esp_err_t iis3dwb_read_temperature(iis3dwb_handle_t *handle, float *temperature);

/**
 * @brief Software reset
 *
 * @param handle Pointer to device handle
 * @return
 *     - ESP_OK: Success
 *     - ESP_FAIL: Communication error
 */
esp_err_t iis3dwb_software_reset(iis3dwb_handle_t *handle);

/**
 * @brief Read single register
 *
 * @param handle Pointer to device handle
 * @param reg Register address
 * @param value Pointer to store register value
 * @return
 *     - ESP_OK: Success
 *     - ESP_FAIL: Communication error
 */
esp_err_t iis3dwb_read_register(iis3dwb_handle_t *handle, uint8_t reg, uint8_t *value);

/**
 * @brief Write single register
 *
 * @param handle Pointer to device handle
 * @param reg Register address
 * @param value Value to write
 * @return
 *     - ESP_OK: Success
 *     - ESP_FAIL: Communication error
 */
esp_err_t iis3dwb_write_register(iis3dwb_handle_t *handle, uint8_t reg, uint8_t value);

#ifdef __cplusplus
}
#endif

#endif /* IIS3DWB_H */
