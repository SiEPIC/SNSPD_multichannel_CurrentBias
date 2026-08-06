/**
 * @file channel.cpp
 * @brief Implementation of the Channel state machine (DAC set + ADC readback).
 */

#include <esp_log.h>
#include <esp_timer.h>
#include <nvs_flash.h>
#include <nvs.h>
#include <math.h>
#include "coordinator.hpp"
#include "channel.hpp"
#include "task_comms.hpp"


static constexpr const char* TAG = "Channel";

DataLog data;
extern UserCmd cmd;


Channel::Channel()
  : channel_id(0),
    mode(Mode::IDLE),
    done(false) {}


bool Channel::init(uint8_t id, ad717x_dev* adc_dev, ad5761r_dev* dac_dev){

  if (adc_dev == nullptr || dac_dev == nullptr) return false;

  channel_id = id;
  channel_odr = ODR_DEFAULT;
  adc_dev_ = adc_dev;
  dac_dev_ = dac_dev;

  current_sense_data_ = load_calibration();
  ad5761r_write_update_dac_register(dac_dev_, voltage_to_bin(0.0f));

  adc_internal_calibration();
  ad717x_set_channel_status(adc_dev_, channel_id, false);

  return true;
}


void Channel::update(const UserCmd& cmd){

  switch (cmd.mode){

    case Mode::IDLE:
      if (mode != Mode::IDLE) {
        ESP_LOGI(TAG, "Ch%d stopped", channel_id);
        stop();
      }
      break;

    case Mode::CALIBRATION:
      set_calibration(cmd); 
      break;

    case Mode::ODR:
      set_odr(cmd.param.odr_setting);
      adc_internal_calibration();
      break;

    case Mode::SWEEP:
      mode = Mode::SWEEP;
      
      ad717x_set_channel_status(adc_dev_, channel_id, true);
      set_sweep_config(cmd);
      break;

    case Mode::STEADY:
      mode = Mode::STEADY;

      ad717x_set_channel_status(adc_dev_, channel_id, true);
      time_to_us(cmd);
      steady_.voltage_ = cmd.param.Steady.voltage;
      steady_.initialized_ = false;
      break;
  }
}


void Channel::steady_run(){

  if(!steady_.initialized_){

    steady_.initialized_ = true;
    ad5761r_write_update_dac_register(dac_dev_, voltage_to_bin(steady_.voltage_));

    if(steady_.timer_en_){
      steady_.finish_time_ = esp_timer_get_time() + steady_.duration_us_; 
    }
  }

  if (steady_.timer_en_ && esp_timer_get_time() > steady_.finish_time_){
    ESP_LOGI(TAG, "Ch%d steady timer done", channel_id);
    stop();
  } 
}


void Channel::sweep_run(){

  if (sweep_.next_step_rdy){

    sweep_.next_step_rdy = false;
    ad5761r_write_update_dac_register(dac_dev_, voltage_to_bin(sweep_.voltage_in_V_));

    switch (sweep_.phase_){

      case SweepPhase::FIRST:

        if (sweep_.voltage_in_V_ < sweep_.range_in_V_){
          sweep_.voltage_in_V_ += sweep_.step_size_V_;
        }
        else{
          sweep_.phase_ = SweepPhase::SECOND;
        }
        break;

      case SweepPhase::SECOND:

        if (sweep_.voltage_in_V_ > - sweep_.range_in_V_){
          sweep_.voltage_in_V_ -= sweep_.step_size_V_;
        }
        else{
          sweep_.phase_ = SweepPhase::THIRD;
        }
        break;

      case SweepPhase::THIRD:

        if (sweep_.voltage_in_V_ < 0){
          sweep_.voltage_in_V_ += sweep_.step_size_V_;
        }
        else{
          ESP_LOGI(TAG, "Ch%d sweep done", channel_id);
          stop();
        }
        break;
    }
    
  }
}


void Channel::stop(){

  ad5761r_write_update_dac_register(dac_dev_, voltage_to_bin(0.0f));
  done = true;
  mode = Mode::IDLE;

  data = {
    .channel_id = channel_id,
    .mode = mode,
  };
  xQueueSend(data_queue, &data, 0);

  ad717x_set_channel_status(adc_dev_, channel_id, false);
  ESP_LOGI(TAG,"CH%d Going back to standby", channel_id);
}


uint16_t Channel::voltage_to_bin(float voltage){
  return (uint16_t)((voltage / current_sense_data_.dac_vref + 2.0) * 65535.0 / 4.0);
}


float Channel::bin_to_voltage(uint32_t bin){
  return ADC_VREF * (static_cast<float>((static_cast<int32_t>(bin) - 0x800000)) 
      * static_cast<float>(0x400000) / ADC_GAIN) / (0.75 * DECI_24BIT);
}


void Channel::compute_and_send_current(uint32_t adc_data){

  if (mode == Mode::IDLE) return;

  float opamp_gain = 1 + 50000.0 / current_sense_data_.r_gain;
  float current_in_uA = bin_to_voltage(adc_data) / (current_sense_data_.r_1k * opamp_gain) * 1e6f;

  data = {
    .channel_id = channel_id,
    .mode = mode,
    .voltage = steady_.voltage_,
    .current = current_in_uA,
    .time = esp_timer_get_time() / 1e6f,
  };

  if (mode == Mode::SWEEP){
    sweep_.next_step_rdy = true;
    data.voltage = sweep_.voltage_in_V_;
  } 

  xQueueSend(data_queue, &data, 0);
}


void Channel::time_to_us(const UserCmd& cmd){

  switch(cmd.param.Steady.time_unit){

    case TimeUnit::MIN:
      steady_.duration_us_ = (int64_t)((double)cmd.param.Steady.duration * 60.0 * 1e6f);
      steady_.timer_en_ = true;
      break;

    case TimeUnit::HOUR:
      steady_.duration_us_ = (int64_t)((double)cmd.param.Steady.duration * 3600.0 * 1e6f);
      steady_.timer_en_ = true;
      break;

    case TimeUnit::DAY:
      steady_.duration_us_ = (int64_t)((double)cmd.param.Steady.duration * 86400.0 * 1e6f);
      steady_.timer_en_ = true;
      break;

    case TimeUnit::MONTH:
      steady_.duration_us_ = (int64_t)((double)cmd.param.Steady.duration * 2678400.0 * 1e6f);
      steady_.timer_en_ = true;
      break;

    default:
      steady_.timer_en_ = false;
      break;
  }
}


void Channel::set_odr(float new_odr){

  if (channel_odr == new_odr) return; 
 
  ad717x_set_channel_status(adc_dev_, channel_id, true);
  AD717X_WaitForReady(adc_dev_, AD717X_CONV_TIMEOUT);
  for (const odrLut& entry : odr_lut){
    if (entry.rate == new_odr){
      ad717x_configure_device_odr(adc_dev_, channel_id, entry.odr);
      AD717X_WaitForReady(adc_dev_, AD717X_CONV_TIMEOUT);
      ad717x_set_channel_status(adc_dev_, channel_id, false);
      return;
    }
  }
   
  ad717x_configure_device_odr(adc_dev_, channel_id, sps_10);
  ESP_LOGW(TAG, "Invalid ODR. Using default: 10 SPS");
  AD717X_WaitForReady(adc_dev_, AD717X_CONV_TIMEOUT);
  ad717x_set_channel_status(adc_dev_, channel_id, false);
  return;
}


void Channel::set_sweep_config(const UserCmd& cmd){

  sweep_.phase_ = SweepPhase::FIRST; 
  sweep_.range_in_V_ = cmd.param.Sweep.range_in_V; 
  sweep_.step_size_V_ = cmd.param.Sweep.step_size / 1000;
  sweep_.single_sweep_steps_ = sweep_.range_in_V_ / sweep_.step_size_V_;
  sweep_.voltage_in_V_ = 0.0f;
  sweep_.next_step_rdy = false;
}


void Channel::adc_internal_calibration(){

  bool ch_en[NUM_CHANNELS];

  for (uint8_t i = 0; i < NUM_CHANNELS; i++){
    ch_en[i] = adc_dev_->chan_map[i].channel_enable;
    if (ch_en[i]) ad717x_set_channel_status(adc_dev_, i, false);
  }
  ad717x_set_channel_status(adc_dev_, channel_id, true);

  AD717X_WaitForReady(adc_dev_, AD717X_CONV_TIMEOUT);
  ad717x_st_reg* adcmode = AD717X_GetReg(adc_dev_, AD717X_ADCMODE_REG);
  adcmode->value &= ~AD717X_ADCMODE_REG_MODE_MSK;
  adcmode->value |= AD717X_ADCMODE_REG_MODE(INTERNAL_OFFSET_CALIB);
  if (AD717X_WriteRegister(adc_dev_, AD717X_ADCMODE_REG) < 0) {
    ESP_LOGE(TAG, "ADC Internal offset calibration for ch%u failed", channel_id);
  }

  AD717X_WaitForReady(adc_dev_, AD717X_CONV_TIMEOUT);
  adcmode = AD717X_GetReg(adc_dev_, AD717X_ADCMODE_REG);
  adcmode->value &= ~AD717X_ADCMODE_REG_MODE_MSK;
  adcmode->value |= AD717X_ADCMODE_REG_MODE(INTERNAL_GAIN_CALIB);
  if (AD717X_WriteRegister(adc_dev_, AD717X_ADCMODE_REG) < 0) {
    ESP_LOGE(TAG, "ADC Internal gain calibration for ch%u failed", channel_id);
  }

  for (uint8_t i = 0; i < NUM_CHANNELS; i++){
   if (i != channel_id && ch_en[i]) ad717x_set_channel_status(adc_dev_, i, true);
  }

  AD717X_WaitForReady(adc_dev_, AD717X_CONV_TIMEOUT);
  adcmode = AD717X_GetReg(adc_dev_, AD717X_ADCMODE_REG);
  adcmode->value &= ~AD717X_ADCMODE_REG_MODE_MSK;
  adcmode->value |= AD717X_ADCMODE_REG_MODE(CONTINUOUS);
  if (AD717X_WriteRegister(adc_dev_, AD717X_ADCMODE_REG) < 0) {
    ESP_LOGE(TAG, "Continuous mode transition for ch%u failed", channel_id);
  }
}

auto Channel::load_calibration() -> current_sense_t {
  
  current_sense_t cal = { R_1K_DEFAULT, R_GAIN_DEFAULT, DAC_VREF_DEFAULT};

  char nvs_key[16];
  snprintf(nvs_key, sizeof(nvs_key), "cal_ch%u", channel_id);

  nvs_handle_t handle;
  esp_err_t err = nvs_open("calibration", NVS_READONLY, &handle);
  if (err != ESP_OK) {
    ESP_LOGI(TAG, "Could not open NVS. Using default values");
    return cal;
  }

  size_t size = sizeof(cal);
  err = nvs_get_blob(handle, nvs_key, &cal, &size);
  if (err != ESP_OK) {
    ESP_LOGI(TAG, "Ch%u: No previous calibration record. Using default values", channel_id);
    nvs_close(handle);
    return cal;
  }

  nvs_close(handle);
  return cal;
}


void Channel::set_calibration(const UserCmd& cmd){
  ESP_LOGI(TAG, "reached here");

  float r1k = cmd.param.Cal.R_1k;
  float rg = cmd.param.Cal.R_gain;
  float vref = cmd.param.Cal.dac_vref;
  
  if(isnan(r1k) || isnan(rg) || isnan(vref)) {
    ESP_LOGE(TAG, "All values must be filled for a channel calibration");
    return;
  }

  if (r1k < R_1K_DEFAULT * (1.0 - R_1K_TOL) || r1k > R_1K_DEFAULT * (1.0 + R_1K_TOL)){
    ESP_LOGE(TAG, "1k resistor value is not within %.2f%% tolerance", R_1K_TOL * 100);
    return;
  }

  else if (rg < R_GAIN_DEFAULT * (1.0 - R_GAIN_TOL) || rg > R_GAIN_DEFAULT * (1.0 + R_GAIN_TOL)){
    ESP_LOGE(TAG, "Op-amp gain resistor value is not within %.2f%% tolerance", R_GAIN_TOL * 100);
    return;
  }

  else if (vref < DAC_VREF_DEFAULT * (1.0 - DAC_VREF_TOL) 
           || vref > DAC_VREF_DEFAULT * (1.0 + DAC_VREF_TOL)){
    ESP_LOGE(TAG, "DAC Vref is not within %.2f%% tolerance", DAC_VREF_TOL * 100);
    return;
  }

  current_sense_data_ = {.r_1k = r1k, .r_gain = rg, .dac_vref = vref};
  ESP_LOGI(TAG, "set vref: %.5f", current_sense_data_.dac_vref);

  char nvs_key[16];
  snprintf(nvs_key, sizeof(nvs_key), "cal_ch%u", channel_id);

  nvs_handle_t handle;
  esp_err_t err = nvs_open("calibration",NVS_READWRITE, &handle);
  if (err != ESP_OK) {

    ESP_LOGE(TAG, "Could not open NVS with namespace");
    return;
  }

  err = nvs_set_blob(handle, nvs_key, &current_sense_data_, sizeof(current_sense_data_));
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "NVS content could not be set with key: %s", nvs_key);
    nvs_close(handle);
    return;
  }
  
  err = nvs_commit(handle);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "NVS could not save calibration value correctly");
    nvs_close(handle);
    return;
  }

  nvs_close(handle);
  ESP_LOGI(TAG, "Calibration successful");
}

