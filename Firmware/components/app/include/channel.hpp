/**
 * @file channel.hpp
 * @brief Per-channel state machine controlling one DAC output and its ADC readback.
 * @details Each Channel owns one AD5761R DAC and shares the AD7172-2 ADC.
 *          The coordinator dispatches mode changes via update() and drives
 *          the per-iteration work with steady_run() / sweep_run(). Sample
 *          records are pushed to data_queue for the log task.
 */

#pragma once

extern "C" {
  #include "ad717x.h"
  #include "ad5761r.h"
}

#include "task_comms.hpp"

constexpr const float ADC_VREF = 2.5; //Internal reference voltage
constexpr const float ADC_OFFSET = static_cast<float>(0x800000);
constexpr const float ADC_GAIN = static_cast<float>(0x555555);
constexpr const float DECI_24BIT = static_cast<float>(0x800000);

constexpr const float DAC_VREF_DEFAULT = 2.5; //External reference voltage
constexpr const float R_1K_DEFAULT = 1000.0;
constexpr const float R_GAIN_DEFAULT = 401.0;

constexpr const float DAC_VREF_TOL = 0.01; // 0.5% Tolerance
constexpr const float R_1K_TOL = 0.01; // 1% Tolerance
constexpr const float R_GAIN_TOL = 0.01; // 1% Tolerance

constexpr const float ODR_DEFAULT = 10.0; 

/**
 * @brief Phases of a triangular voltage sweep.
 * @details FIRST: 0 → +range. SECOND: +range → −range. THIRD: −range → 0.
 */
enum class SweepPhase {FIRST, SECOND, THIRD};

/**
 * @brief One output channel: DAC voltage set + ADC current readback.
 */
class Channel{

  public:

    Channel();
    uint8_t channel_id;   
    Mode mode;            
    float channel_odr;
    bool done;            

    /**
     * @brief Bind hardware handles and drive DAC to 0 V.
     * @param id Channel index (0 .. NUM_CHANNELS-1).
     * @param adc_dev Shared AD7172-2 device handle.
     * @param dac_dev This channel's AD5761R device handle.
     * @return true on success, false if either handle is null.
     */
    bool init(uint8_t id, ad717x_dev* adc_dev, ad5761r_dev* dac_dev);

    /**
     * @brief Apply a new UserCmd — switches mode and resets per-mode state.
     * @param cmd Parsed user command targeting this channel.
     */
    void update(const UserCmd& cmd);

    void compute_and_send_current(uint32_t adc_data);

    /** @brief One sweep step: set next DAC voltage, sample current, advance phase. */
    void sweep_run();

    /** @brief One steady iteration: initialize DAC on first call, then sample current. */
    void steady_run();


    /** @brief Drop DAC to 0 V, mark channel OFF, emit a final DataLog record. */
    void stop();

  private:

    struct odrLut{
      float rate;
      ad717x_odr odr;
    };

    static constexpr odrLut odr_lut[] = {
      { 1.25,  sps_1p25 },
      { 2.5,   sps_2p5  },
      { 5.0,   sps_5    },
      { 10.0,  sps_10   },
      { 16.0,  sps_16   },
      { 20.0,  sps_20   },
      { 49.0,  sps_49   },
      { 59.0,  sps_59   },
      { 100.0, sps_100  },
      { 200.0, sps_200  },
    };

    struct SteadyParams{
      bool initialized_ = false;
      bool timer_en_ = false;
      float voltage_ = 0.0f;
      int64_t duration_us_ = 0;
      int64_t finish_time_ = 0;
    };

    struct SweepParams{
      SweepPhase phase_ = SweepPhase::FIRST;
      float range_in_V_ = 0.0f;
      float step_size_V_ = 0.0f;
      float voltage_in_V_ = 0.0f;
      uint32_t single_sweep_steps_ = 0;
      uint32_t total_steps_ = 0;
      bool next_step_rdy = false;
    };

    struct current_sense_t{
      float r_1k;
      float r_gain;
      float dac_vref;
    };


    current_sense_t current_sense_data_;
    SteadyParams steady_;
    SweepParams sweep_;

    ad717x_dev* adc_dev_;
    ad5761r_dev* dac_dev_;

    int32_t adc_raw_data;


    /** @brief Convert cmd's duration+time_unit into FreeRTOS ticks; set steady_.timer_en_. */
    void time_to_us(const UserCmd& cmd);

    /**
     * @brief Convert a voltage (V, ±5V range) to an AD5761R 16-bit code.
     * @param voltage Desired DAC output in volts.
     * @return 16-bit DAC input code.
     */
    uint16_t voltage_to_bin(float voltage);

    /**
     * @brief Convert a 24-bit AD7172-2 sample to volts at the ADC input pin.
     * @param bin Offset-binary 24-bit code from the ADC.
     * @return Voltage in volts (post-PGA, pre-opamp gain).
     */
    float bin_to_voltage(uint32_t bin);

    float get_opamp_gain(void);

    void adc_internal_calibration(void);

    current_sense_t load_calibration(void);

    void set_calibration(const UserCmd& cmd);

    void set_odr(float odr);

    void set_sweep_config(const UserCmd& cmd);

};
