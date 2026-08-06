/**
 * @file no_os_esp32_spi.c
 * @brief Implementation of the no_os_spi platform_ops backed by ESP-IDF SPI master.
 */

#include <esp_log.h>
#include <esp_err.h>
#include <driver/gpio.h>
#include <driver/spi_master.h>
#include "no_os_spi.h"
#include "no_os_alloc.h"
#include "spi_configs.h"
#include "no_os_esp32_spi.h"

#define TAG "ESP32-SPI"

int32_t esp32_spi_init(struct no_os_spi_desc **desc, const struct no_os_spi_init_param *param){

  if (!param) {
    ESP_LOGE(TAG, "null param");
    return -1;
  } 

  struct no_os_spi_desc *d = no_os_calloc(1, sizeof(*d));
  spi_device_handle_t *handle = no_os_calloc(1, sizeof(*handle));

  if (!d || !handle) {
    ESP_LOGE(TAG, "Not enough memory");
    no_os_free(d);
    no_os_free(handle);
    return -1;
  }

  d->chip_select = param->chip_select;
  esp32_spi_config_t *esp32_spi_config = (esp32_spi_config_t *)param->extra; 

  // CS pins are manually driven due to the esp32's max 3 spi CS restriction
  spi_device_interface_config_t dev_config = {
    .clock_speed_hz = param->max_speed_hz,
    .spics_io_num = -1, 
    .mode = param->mode,
    .queue_size = 1,
    .input_delay_ns = 50,
    .cs_ena_pretrans = 2,
    .cs_ena_posttrans = 2,
  };

  if (spi_bus_add_device(esp32_spi_config->host, &dev_config, handle) != ESP_OK) {
    ESP_LOGE(TAG, "Failed to add SPI device");
    no_os_free(d);
    no_os_free(handle);
    return -1;
  }

  d->extra = handle;
  *desc = d;
  return 0;
}

int32_t esp32_spi_write_read(struct no_os_spi_desc *desc, uint8_t *data, uint16_t len){

  if (!desc || !data || len == 0) return -1;

  spi_device_handle_t *handle = (spi_device_handle_t *)desc->extra;

  //This sets the correct SCLK idle level for different SPI mode devices (ADC: 3, DAC: 1)
  uint8_t dummy;
  spi_transaction_t nop = {.length = 8, .tx_buffer = &dummy, .rx_buffer = NULL};
  spi_device_polling_transmit(*handle, &nop);

  gpio_set_level(desc->chip_select, 0);
  spi_transaction_t transaction = {
    .length = 8 * len,
    .tx_buffer = data,
    .rx_buffer = data
  };

  int tra = spi_device_polling_transmit(*handle, &transaction);
  gpio_set_level(desc->chip_select, 1);

  return  tra== ESP_OK ? 0 : -1;
}


int32_t esp32_spi_remove(struct no_os_spi_desc *desc){
  if (!desc) return -1;

  gpio_set_level(desc->chip_select, 1);
  spi_device_handle_t *handle = (spi_device_handle_t *)desc->extra;
  spi_bus_remove_device(*handle);
  no_os_free(handle);
  no_os_free(desc);

  return 0;
}


