/**
 * @file no_os_esp32_spi.h
 * @brief ESP32 backend implementing the no_os_spi platform_ops API.
 * @details Wired into no_os_spi drivers via `platform_ops` in spi_configs.c so
 *          the vendor AD5761R / AD717x drivers can talk SPI through ESP-IDF.
 */

#pragma once

#include <no_os_spi.h>
#include <driver/gpio.h>
#include <driver/spi_master.h>


/**
 * @brief Add an SPI device to the shared bus and populate a no_os descriptor.
 * @param desc [out] Newly allocated no_os_spi_desc; caller frees via esp32_spi_remove.
 * @param param [in] Chip select, clock rate, and SPI mode from the vendor init struct.
 * @return 0 on success, -1 on allocation or bus-add failure.
 */
int32_t esp32_spi_init(struct no_os_spi_desc **desc, const struct no_os_spi_init_param *param);

/**
 * @brief Full-duplex SPI transaction with manual CS toggle; rx overwrites tx buffer.
 * @param desc [in] Device descriptor from esp32_spi_init.
 * @param data [in,out] Bytes to transmit; overwritten with received bytes on return.
 * @param data_len Number of bytes to transfer.
 * @return 0 on success, -1 on invalid args or SPI failure.
 */
int32_t esp32_spi_write_read(struct no_os_spi_desc *desc, uint8_t *data, uint16_t data_len);

/**
 * @brief Release the SPI device and free its descriptor.
 * @param desc Descriptor obtained from esp32_spi_init.
 * @return 0 on success, -1 on null descriptor.
 */
int32_t esp32_spi_remove(struct no_os_spi_desc *);
