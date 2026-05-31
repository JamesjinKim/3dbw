/**
 * @file iis3dwb.c
 * @brief IIS3DWB 3-axis Digital Vibration Sensor Driver Implementation
 */

#include "iis3dwb.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <string.h>

static const char *TAG = "IIS3DWB";

/* SPI Read/Write bit */
#define SPI_READ_BIT    0x80
#define SPI_WRITE_BIT   0x00

/* ============================================================================
 * Internal Helper Functions
 * ============================================================================ */

/**
 * @brief Get sensitivity value based on full-scale setting
 */
static float get_sensitivity(iis3dwb_fs_xl_t fs)
{
    switch (fs) {
        case IIS3DWB_FS_2G:  return IIS3DWB_SENSITIVITY_2G;
        case IIS3DWB_FS_4G:  return IIS3DWB_SENSITIVITY_4G;
        case IIS3DWB_FS_8G:  return IIS3DWB_SENSITIVITY_8G;
        case IIS3DWB_FS_16G: return IIS3DWB_SENSITIVITY_16G;
        default:            return IIS3DWB_SENSITIVITY_2G;
    }
}

/* ============================================================================
 * Low-Level SPI Communication
 * ============================================================================ */

esp_err_t iis3dwb_read_register(iis3dwb_handle_t *handle, uint8_t reg, uint8_t *value)
{
    if (!handle || !handle->initialized || !value) {
        return ESP_ERR_INVALID_ARG;
    }

    spi_transaction_t trans = {
        .flags = SPI_TRANS_USE_RXDATA | SPI_TRANS_USE_TXDATA,
        .length = 16,  /* 8 bits address + 8 bits data */
        .tx_data = {reg | SPI_READ_BIT, 0x00},
    };

    esp_err_t ret = spi_device_polling_transmit(handle->spi_handle, &trans);
    if (ret == ESP_OK) {
        *value = trans.rx_data[1];
    }

    return ret;
}

esp_err_t iis3dwb_write_register(iis3dwb_handle_t *handle, uint8_t reg, uint8_t value)
{
    if (!handle || !handle->initialized) {
        return ESP_ERR_INVALID_ARG;
    }

    spi_transaction_t trans = {
        .flags = SPI_TRANS_USE_TXDATA,
        .length = 16,  /* 8 bits address + 8 bits data */
        .tx_data = {reg | SPI_WRITE_BIT, value},
    };

    return spi_device_polling_transmit(handle->spi_handle, &trans);
}

/**
 * @brief Read multiple bytes from consecutive registers
 */
static esp_err_t iis3dwb_read_registers(iis3dwb_handle_t *handle, uint8_t reg,
                                         uint8_t *data, size_t len)
{
    if (!handle || !handle->initialized || !data || len == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    /* Allocate buffers for SPI transaction */
    uint8_t *tx_buf = heap_caps_calloc(1, len + 1, MALLOC_CAP_DMA);
    uint8_t *rx_buf = heap_caps_calloc(1, len + 1, MALLOC_CAP_DMA);
    if (!tx_buf || !rx_buf) {
        free(tx_buf);
        free(rx_buf);
        return ESP_ERR_NO_MEM;
    }

    tx_buf[0] = reg | SPI_READ_BIT;

    spi_transaction_t trans = {
        .length = (len + 1) * 8,
        .tx_buffer = tx_buf,
        .rx_buffer = rx_buf,
    };

    esp_err_t ret = spi_device_polling_transmit(handle->spi_handle, &trans);
    if (ret == ESP_OK) {
        memcpy(data, &rx_buf[1], len);
    }

    free(tx_buf);
    free(rx_buf);

    return ret;
}

/* ============================================================================
 * Sensor Control Functions
 * ============================================================================ */

esp_err_t iis3dwb_init(const iis3dwb_config_t *config, iis3dwb_handle_t *handle)
{
    if (!config || !handle) {
        ESP_LOGE(TAG, "Invalid arguments");
        return ESP_ERR_INVALID_ARG;
    }

    memset(handle, 0, sizeof(iis3dwb_handle_t));

    ESP_LOGI(TAG, "Initializing IIS3DWB vibration sensor...");

    /* Configure SPI bus */
    spi_bus_config_t bus_cfg = {
        .mosi_io_num = config->mosi_io_num,
        .miso_io_num = config->miso_io_num,
        .sclk_io_num = config->sclk_io_num,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 4096,
    };

    esp_err_t ret = spi_bus_initialize(config->spi_host, &bus_cfg, SPI_DMA_CH_AUTO);
    if (ret != ESP_OK && ret != ESP_ERR_INVALID_STATE) {
        /* ESP_ERR_INVALID_STATE means bus already initialized, which is OK */
        ESP_LOGE(TAG, "Failed to initialize SPI bus: %s", esp_err_to_name(ret));
        return ret;
    }

    /* Configure SPI device */
    spi_device_interface_config_t dev_cfg = {
        .clock_speed_hz = config->clk_speed_hz,
        .mode = 3,  /* CPOL=1, CPHA=1 - SPI Mode 3 */
        .spics_io_num = config->cs_io_num,
        .queue_size = 1,
        .pre_cb = NULL,
        .post_cb = NULL,
    };

    ret = spi_bus_add_device(config->spi_host, &dev_cfg, &handle->spi_handle);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to add SPI device: %s", esp_err_to_name(ret));
        return ret;
    }

    handle->initialized = true;

    /* Short delay after power-up */
    vTaskDelay(pdMS_TO_TICKS(10));

    /* Verify device ID */
    uint8_t device_id = 0;
    ret = iis3dwb_get_device_id(handle, &device_id);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to read WHO_AM_I register");
        handle->initialized = false;
        return ESP_FAIL;
    }

    if (device_id != IIS3DWB_DEVICE_ID) {
        ESP_LOGE(TAG, "Invalid device ID: 0x%02X (expected 0x%02X)",
                 device_id, IIS3DWB_DEVICE_ID);
        handle->initialized = false;
        return ESP_ERR_NOT_FOUND;
    }

    ESP_LOGI(TAG, "Device ID verified: 0x%02X", device_id);

    /* Software reset */
    ret = iis3dwb_software_reset(handle);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Software reset failed");
        return ret;
    }

    /* Wait for reset to complete */
    vTaskDelay(pdMS_TO_TICKS(20));

    /* Configure CTRL3_C: BDU enable, auto-increment enable */
    ret = iis3dwb_write_register(handle, IIS3DWB_REG_CTRL3_C,
                                  IIS3DWB_CTRL3_C_BDU | IIS3DWB_CTRL3_C_IF_INC);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to configure CTRL3_C");
        return ret;
    }

    /* Set full-scale */
    ret = iis3dwb_set_full_scale(handle, config->full_scale);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to set full-scale");
        return ret;
    }

    /* Set bandwidth */
    ret = iis3dwb_set_bandwidth(handle, config->bandwidth);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to set bandwidth");
        return ret;
    }

    /* Enable accelerometer */
    ret = iis3dwb_enable(handle, true);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to enable accelerometer");
        return ret;
    }

    ESP_LOGI(TAG, "IIS3DWB initialized successfully");
    ESP_LOGI(TAG, "  SPI Clock: %d Hz", config->clk_speed_hz);
    ESP_LOGI(TAG, "  Full-scale: %s",
             config->full_scale == IIS3DWB_FS_2G ? "±2g" :
             config->full_scale == IIS3DWB_FS_4G ? "±4g" :
             config->full_scale == IIS3DWB_FS_8G ? "±8g" : "±16g");
    ESP_LOGI(TAG, "  ODR: 26.667 kHz (fixed)");

    return ESP_OK;
}

esp_err_t iis3dwb_deinit(iis3dwb_handle_t *handle)
{
    if (!handle || !handle->initialized) {
        return ESP_ERR_INVALID_ARG;
    }

    /* Disable accelerometer (power-down) */
    iis3dwb_enable(handle, false);

    /* Remove SPI device */
    esp_err_t ret = spi_bus_remove_device(handle->spi_handle);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to remove SPI device: %s", esp_err_to_name(ret));
        return ret;
    }

    handle->initialized = false;
    ESP_LOGI(TAG, "IIS3DWB deinitialized");

    return ESP_OK;
}

esp_err_t iis3dwb_get_device_id(iis3dwb_handle_t *handle, uint8_t *device_id)
{
    return iis3dwb_read_register(handle, IIS3DWB_REG_WHO_AM_I, device_id);
}

esp_err_t iis3dwb_software_reset(iis3dwb_handle_t *handle)
{
    if (!handle || !handle->initialized) {
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t ctrl3_c;
    esp_err_t ret = iis3dwb_read_register(handle, IIS3DWB_REG_CTRL3_C, &ctrl3_c);
    if (ret != ESP_OK) {
        return ret;
    }

    /* Set SW_RESET bit */
    ctrl3_c |= IIS3DWB_CTRL3_C_SW_RESET;
    ret = iis3dwb_write_register(handle, IIS3DWB_REG_CTRL3_C, ctrl3_c);
    if (ret != ESP_OK) {
        return ret;
    }

    /* Wait for reset to complete (SW_RESET bit auto-clears) */
    uint8_t timeout = 100;
    while (timeout--) {
        vTaskDelay(pdMS_TO_TICKS(1));
        ret = iis3dwb_read_register(handle, IIS3DWB_REG_CTRL3_C, &ctrl3_c);
        if (ret == ESP_OK && !(ctrl3_c & IIS3DWB_CTRL3_C_SW_RESET)) {
            break;
        }
    }

    if (timeout == 0) {
        ESP_LOGW(TAG, "Software reset timeout");
    }

    return ESP_OK;
}

esp_err_t iis3dwb_enable(iis3dwb_handle_t *handle, bool enable)
{
    if (!handle || !handle->initialized) {
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t ctrl1_xl;
    esp_err_t ret = iis3dwb_read_register(handle, IIS3DWB_REG_CTRL1_XL, &ctrl1_xl);
    if (ret != ESP_OK) {
        return ret;
    }

    if (enable) {
        ctrl1_xl |= IIS3DWB_CTRL1_XL_XL_EN;
    } else {
        ctrl1_xl &= ~IIS3DWB_CTRL1_XL_XL_EN;
    }

    return iis3dwb_write_register(handle, IIS3DWB_REG_CTRL1_XL, ctrl1_xl);
}

esp_err_t iis3dwb_set_full_scale(iis3dwb_handle_t *handle, iis3dwb_fs_xl_t fs)
{
    if (!handle || !handle->initialized) {
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t ctrl1_xl;
    esp_err_t ret = iis3dwb_read_register(handle, IIS3DWB_REG_CTRL1_XL, &ctrl1_xl);
    if (ret != ESP_OK) {
        return ret;
    }

    /* Clear FS bits and set new value */
    ctrl1_xl &= ~0x0C;  /* Clear bits 3:2 */
    ctrl1_xl |= (fs & 0x0C);

    ret = iis3dwb_write_register(handle, IIS3DWB_REG_CTRL1_XL, ctrl1_xl);
    if (ret == ESP_OK) {
        handle->full_scale = fs;
        handle->sensitivity = get_sensitivity(fs);
    }

    return ret;
}

esp_err_t iis3dwb_set_bandwidth(iis3dwb_handle_t *handle, iis3dwb_bw_xl_t bw)
{
    if (!handle || !handle->initialized) {
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t ctrl6_c;
    esp_err_t ret = iis3dwb_read_register(handle, IIS3DWB_REG_CTRL6_C, &ctrl6_c);
    if (ret != ESP_OK) {
        return ret;
    }

    /* Clear bandwidth bits and set new value */
    ctrl6_c &= ~0x07;  /* Clear bits 2:0 */
    ctrl6_c |= (bw & 0x07);

    return iis3dwb_write_register(handle, IIS3DWB_REG_CTRL6_C, ctrl6_c);
}

/* ============================================================================
 * Data Reading Functions
 * ============================================================================ */

esp_err_t iis3dwb_data_ready(iis3dwb_handle_t *handle, bool *available)
{
    if (!handle || !handle->initialized || !available) {
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t status;
    esp_err_t ret = iis3dwb_read_register(handle, IIS3DWB_REG_STATUS_REG, &status);
    if (ret == ESP_OK) {
        *available = (status & IIS3DWB_STATUS_XLDA) != 0;
    }

    return ret;
}

esp_err_t iis3dwb_read_raw_data(iis3dwb_handle_t *handle, iis3dwb_raw_data_t *raw_data)
{
    if (!handle || !handle->initialized || !raw_data) {
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t data[6];
    esp_err_t ret = iis3dwb_read_registers(handle, IIS3DWB_REG_OUTX_L_A, data, 6);
    if (ret == ESP_OK) {
        raw_data->x = (int16_t)(data[1] << 8 | data[0]);
        raw_data->y = (int16_t)(data[3] << 8 | data[2]);
        raw_data->z = (int16_t)(data[5] << 8 | data[4]);
    }

    return ret;
}

esp_err_t iis3dwb_read_accel_data(iis3dwb_handle_t *handle, iis3dwb_accel_data_t *accel_data)
{
    if (!handle || !handle->initialized || !accel_data) {
        return ESP_ERR_INVALID_ARG;
    }

    iis3dwb_raw_data_t raw;
    esp_err_t ret = iis3dwb_read_raw_data(handle, &raw);
    if (ret == ESP_OK) {
        accel_data->x_mg = raw.x * handle->sensitivity;
        accel_data->y_mg = raw.y * handle->sensitivity;
        accel_data->z_mg = raw.z * handle->sensitivity;
    }

    return ret;
}

esp_err_t iis3dwb_read_temperature(iis3dwb_handle_t *handle, float *temperature)
{
    if (!handle || !handle->initialized || !temperature) {
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t data[2];
    esp_err_t ret = iis3dwb_read_registers(handle, IIS3DWB_REG_OUT_TEMP_L, data, 2);
    if (ret == ESP_OK) {
        int16_t raw_temp = (int16_t)(data[1] << 8 | data[0]);
        *temperature = IIS3DWB_TEMP_OFFSET + (raw_temp / IIS3DWB_TEMP_SENSITIVITY);
    }

    return ret;
}
