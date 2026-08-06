/**
 * @file app_main.cpp
 * @brief ESP-IDF application entry point.
 * @details Creates the inter-task queues and spawns the three FreeRTOS
 *          tasks that make up the firmware: serial I/O input, data-log
 *          output, and the coordinator that drives the DAC/ADC state
 *          machines. I/O tasks are pinned to core 0; the coordinator
 *          owns core 1 to keep control-loop timing off the USB path.
 */

#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include "coordinator.hpp"
#include "task_comms.hpp"


extern "C" void app_main(void) {

  data_queue = xQueueCreate(4, sizeof(DataLog));
  user_cmd_queue = xQueueCreate(4, sizeof(UserCmd));

  xTaskCreatePinnedToCore(log_task, "Log", 7000, NULL, 2, NULL, 0);
  xTaskCreatePinnedToCore(user_cmd_task, "Input", 7000, NULL, 2, NULL, 0);
  xTaskCreatePinnedToCore(Coordinator::coordinator_task, "Coordinator", 10000, NULL, 2, NULL, 1);
}
