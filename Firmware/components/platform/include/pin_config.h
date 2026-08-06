/**
 * @file pin_config.h
 * @brief Board pin assignments and SPI clock rates for the ESP32 ↔ AD5761R/AD7172-2 wiring.
 * @details Chip-selects are driven as regular GPIOs (not native SPI CS) because
 *          the ESP32 SPI peripheral supports only 3 hardware CS lines and this
 *          board has 5 devices on one bus (4 DACs + 1 ADC).
 */

#ifndef __PIN_CONFIG_H__
#define __PIN_CONFIG_H__

#include <stdint.h>
#include <driver/gpio.h>

#define SPI_SCLK 18
#define SPI_MOSI 23
#define SPI_MISO 19

#define CS_DAC3 5
#define CS_DAC2 17
#define CS_DAC1 16
#define CS_DAC0 4
#define CS_ADC 21

#define DAC_VREF 2.5
#define D_16BIT 65535
#define M_SLOPE 4
#define C_OFFSET 2
#define NUM_BITS 16

const uint32_t DAC_FREQ = 10000000; //10MHz
const uint32_t ADC_FREQ = 10000000;  //10MHz

#endif


