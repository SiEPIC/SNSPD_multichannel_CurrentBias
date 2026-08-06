/**
 * @file coordinator.hpp
 * @brief Top-level orchestrator: owns hardware handles, dispatches user commands,
 *        and drives per-channel state machines on the coordinator task.
 * @details Init sequence brings up the shared SPI bus, the ADC (with status-byte
 *          appended to data reads), and every DAC. The run loop drains
 *          user_cmd_queue into the target Channel, then iterates all channels
 *          to run one step of their current mode. Watchdog is reset each pass.
 */

#pragma once

extern "C" {
  #include "ad717x.h"
  #include "ad5761r.h"
}

#include <nvs_flash.h>
#include <nvs.h>
#include <driver/spi_master.h>
#include "channel.hpp"


static constexpr uint8_t NUM_CHANNELS = 4;

/**
 * @brief Owns hardware handles and the Channel array; runs the control loop.
 */
class Coordinator{

  public:
    Coordinator();

    /**
     * @brief FreeRTOS task entry: init hardware, register with task WDT, run loop.
     * @param arg Unused; required by FreeRTOS task signature.
     */
    static void coordinator_task(void* arg);



  private:
    no_os_spi_desc *spi_desc_;
    ad717x_dev *adc_dev_;
    ad5761r_dev *dac_dev_[NUM_CHANNELS];
    Channel channel_[NUM_CHANNELS];

    /**
     * @brief Bring up SPI bus, ADC, DACs, and each Channel.
     * @return true on success; false on any peripheral init failure.
     */
    bool init(void);

    /**
     * @brief Main control loop: dispatch commands and step every channel.
     */
    void run(void);

    void poll_adc_dispatch_to_channel(void);

    void nvs_init(void);
};
