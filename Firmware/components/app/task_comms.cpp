/**
 * @file task_comms.cpp
 * @brief Implementation of the serial-input parser and CSV-out logger tasks.
 */

#include <stdio.h>
#include <cstring>
#include <esp_log.h>
#include <esp_timer.h>
#include "task_comms.hpp"
#include "coordinator.hpp"

#define DEBUG false;

QueueHandle_t event_queue;
QueueHandle_t data_queue;
QueueHandle_t user_cmd_queue;


#ifdef DEBUG
static constexpr const char* TAG = "Comms";
#endif

void user_cmd_task(void* arg){

  UserCmd cmd;
  char buffer[128];
  char* saveptr = nullptr;
  const char delimiter[] = ",";
  int64_t wdt_connection = esp_timer_get_time();

  while(true){

    if (fgets(buffer, sizeof(buffer), stdin) == nullptr){

      //Software watchdog for USB cable connection. 
      //Checks for the signal from laptop sent every 2 seconds  
      if ((esp_timer_get_time() - wdt_connection) > 2.5 * 1e6) {
        for (uint8_t i = 0; i < NUM_CHANNELS; i++){
          cmd.channel_id = i;
          cmd.mode = Mode::IDLE;
          xQueueSend(user_cmd_queue, &cmd, 0);
        }
      }

      vTaskDelay(pdMS_TO_TICKS(20));
      continue;
    }
     
    wdt_connection = esp_timer_get_time();

    buffer[strcspn(buffer, "\r\n")] = '\0';

    char* ch_token;
    ch_token = strtok_r(buffer, delimiter, &saveptr);
    if (!ch_token) continue;
    cmd.channel_id = atoi(ch_token);

    char* mode_tok = strtok_r(nullptr, delimiter, &saveptr);
    if (!mode_tok) continue;

    if (strcmp(mode_tok,"OFF") == 0){
      cmd.mode = Mode::IDLE;
    }

    else if (strcmp(mode_tok, "CALIBRATION") == 0){

      cmd.mode = Mode::CALIBRATION;

      char* r1k_tok = strtok_r(nullptr, delimiter, &saveptr);
      char* rgain_tok = strtok_r(nullptr, delimiter, &saveptr);
      char* dac_vref_tok = strtok_r(nullptr, delimiter, &saveptr);
      if (!r1k_tok || !rgain_tok || !dac_vref_tok) continue;

      cmd.param.Cal.R_1k = atof(r1k_tok);
      cmd.param.Cal.R_gain = atof(rgain_tok);
      cmd.param.Cal.dac_vref = atof(dac_vref_tok);
    }

    else if (strcmp(mode_tok, "ODR") == 0){

      cmd.mode = Mode::ODR;

      char* odr_tok = strtok_r(nullptr, delimiter, &saveptr);
      if (!odr_tok) continue;

      cmd.param.odr_setting = atof(odr_tok);
    }
    
    else if (strcmp(mode_tok,"STEADY") == 0){

      char* voltage_tok = strtok_r(nullptr, delimiter, &saveptr);
      char* duration_tok = strtok_r(nullptr, delimiter, &saveptr);
      if (!voltage_tok || !duration_tok) continue;

      cmd.mode = Mode::STEADY;  
      cmd.param.Steady.voltage = atof(voltage_tok);
      cmd.param.Steady.duration = atof(duration_tok);

      char* time_tok = strtok_r(nullptr, delimiter, &saveptr);
      if (!time_tok) continue;
      
      if(strcmp(time_tok, "Min") == 0){
        cmd.param.Steady.time_unit = TimeUnit::MIN;
      }
      else if(strcmp(time_tok, "Hour") == 0){
        cmd.param.Steady.time_unit = TimeUnit::HOUR;
      }
      else if(strcmp(time_tok, "Day") == 0){
        cmd.param.Steady.time_unit = TimeUnit::DAY;
      }
      else if(strcmp(time_tok, "Month") == 0){
        cmd.param.Steady.time_unit = TimeUnit::MONTH;
      }
    }

    else if (strcmp(mode_tok,"SWEEP") == 0){

      char* range_tok = strtok_r(nullptr, delimiter, &saveptr);
      char* step_tok = strtok_r(nullptr, delimiter, &saveptr);
      if (!range_tok || !step_tok) continue;

      cmd.mode = Mode::SWEEP;  
      cmd.param.Sweep.range_in_V = atof(range_tok);
      cmd.param.Sweep.step_size = atof(step_tok);
    }

    xQueueSend(user_cmd_queue, &cmd, 0);

  }
}


void log_task (void* arg){

  DataLog received_data;

  while(true){
    if(xQueueReceive(data_queue, &received_data, 1) == pdTRUE){
      printf("data, %d, %d, %.3f, %.5f, %lu \n", 
          received_data.channel_id, 
          (int)received_data.mode, 
          received_data.voltage, 
          received_data.current, 
          (unsigned long)received_data.time
      );  
    }
  }
}
