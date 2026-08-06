/**
 * @file task_comms.hpp
 * @brief Shared types and queue handles used by the input/log/coordinator tasks.
 * @details Defines the wire-format command (`UserCmd`) parsed from the
 *          serial input task, the sample record (`DataLog`) produced by
 *          each channel run, and the FreeRTOS queue handles that carry
 *          them between tasks. Mode/TimeUnit enums are shared vocabulary
 *          across the parser, coordinator, and per-channel state machines.
 */

#pragma once

#include <freertos/FreeRTOS.h>
#include <freertos/task.h>


/**
 * @brief Operating mode for a channel.
 * @details Set by the input parser, driven by the coordinator's run loop.
 *          SWEEP steps voltage through a triangular ramp; STEADY holds a
 *          fixed voltage; OFF drops the DAC to 0 V and stops logging.
 */
enum class Mode : uint8_t {SWEEP, STEADY, IDLE, CALIBRATION, ODR};

/**
 * @brief Time unit for STEADY-mode duration.
 * @details INF means run until explicitly turned OFF; the other units are
 *          multiplied by the numeric duration to yield the finish time.
 */
enum class TimeUnit : uint8_t {MIN, HOUR, DAY, MONTH, INF};


/**
 * @brief Parameters for STEADY mode.
 * @details voltage in volts; duration is a scalar interpreted by time_unit.
 */
struct SteadyParams{
  float voltage;
  float duration;
  TimeUnit time_unit;
};


/**
 * @brief Parameters for SWEEP mode.
 * @details range_in_V is the half-amplitude of the triangular ramp
 *          (sweep spans ±range). step_size is in millivolts as sent by
 *          the Python side; the channel converts to volts internally.
 */
struct SweepParams{
  float range_in_V;
  float step_size;
};


struct CalParams{
  bool cal_en;
  float R_1k;
  float R_gain;
  float dac_vref;
};

struct OdrParams{
 bool odr_change;
 float odr_setting;
};


/**
 * @brief Command record produced by the serial parser, consumed by Coordinator.
 * @details param is a union sized by whichever mode is set; only the
 *          member matching `mode` is valid on any given record.
 */
struct UserCmd{
  uint8_t channel_id;
  Mode mode;
  union {
    struct SteadyParams Steady;
    struct SweepParams Sweep;
    struct CalParams Cal;
    float odr_setting;
  } param;
};


/**
 * @brief One sample record emitted per channel per control-loop iteration.
 * @details Populated in Channel::steady_run / sweep_run, sent via
 *          data_queue, printed as CSV by log_task. time is FreeRTOS
 *          ticks converted to milliseconds.
 */
struct DataLog{
  uint8_t channel_id;
  Mode mode;
  float voltage;
  float current;
  float time;
};


/** @brief Queue: Channel → log_task. Depth 4, holds DataLog by value. */
extern QueueHandle_t data_queue;
/** @brief Queue: user_cmd_task → Coordinator. Depth 4, holds UserCmd by value. */
extern QueueHandle_t user_cmd_queue;
/** @brief Reserved for future event routing; currently unused. */
extern QueueHandle_t event_queue;

/**
 * @brief FreeRTOS task: drain data_queue and print each record as CSV to stdout.
 * @param arg Unused; required by FreeRTOS task signature.
 */
void log_task (void* arg);

/**
 * @brief FreeRTOS task: read comma-delimited commands from stdin, parse, and enqueue.
 * @param arg Unused; required by FreeRTOS task signature.
 * @details Accepted formats:
 *          - `<ch>,OFF`
 *          - `<ch>,STEADY,<voltage>,<duration>,<Min|Hour|Day|Month>`
 *          - `<ch>,SWEEP,<range_V>,<step_size_mV>`
 *          Malformed lines are dropped silently.
 */
void user_cmd_task(void* arg);

