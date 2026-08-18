# Using `vs_controller` without the GUI

`vs_controller.py` (at `GUI/src/gui/controller/vs_controller.py`) is
self-contained: its only runtime dependency is `pyserial`. It can be imported
and driven from any Python script — the GUI is one consumer of it, not a
prerequisite.

## Importing

No relative imports are used inside the file, so any of these work:

```python
# Option A — script sits next to a copy of the file
from vs_controller import VoltageSourceController, DataLog

# Option B — reach into the source tree without installing
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(
    "/home/ta1/SNSPD_multichannel_CurrentBias/GUI/src/gui/controller"
)))
from vs_controller import VoltageSourceController, DataLog

# Option C — install the package (`pip install -e .` from the GUI directory)
from gui.controller.vs_controller import VoltageSourceController, DataLog
```

## Lifecycle

```
connect  →  start_reading  →  send_…  →  stop_reading  →  disconnect
```

- `connect` opens the serial port and starts the 2 s cable-watchdog heartbeat.
- `start_reading` spawns the background reader that parses `data,…` /
  `calib,…` lines and populates the per-channel ring buffer.
- `stop_reading` closes any open sweep log and joins the reader.
- `disconnect` stops the heartbeat and closes the port.

If you skip `start_reading`, no telemetry is parsed and `has_new_data` stays
`False`. If you skip `connect` (opening `serial.Serial` yourself), the
firmware zeros every channel after 2.5 s because no heartbeat is running.

## Public API

### Static helpers

- `VoltageSourceController.list_ports() → list[tuple[device, description]]` —
  USB-looking devices sorted first (by VID + keyword score).
- `VoltageSourceController.guess_port() → str` — best pick, or `""` if
  nothing looks USB-ish.

### Connection state

- `is_connected: bool` (property)
- `connected_port: str` (property)
- `active_sweep_channels() → list[int]`

### Command senders

All are fire-and-forget writes terminated with `\0`. Each returns `False`
silently if the port is not open.

- `send_off(ch)`
- `send_steady(ch, voltage, duration, time_unit)`
  Runs channel `ch` at the constant DAC voltage. `time_unit` accepts the
  firmware's timer tokens (`"Sec"`, `"Min"`, `"Hour"`) **or `"Inf"`, which
  runs indefinitely and ignores `duration`**. `"Inf"` is the common case —
  use it unless you specifically want the firmware to auto-turn-off after a
  bounded interval.
- `send_sweep(ch, range_v, step_mv)` — sweeps ±`range_v` in `step_mv` steps.
- `send_calibration(ch, r_1k, r_gain, dac_vref)` — persisted in NVS on the ESP.
- `send_odr(ch, sps)` — ADC output data rate.
- `send_command(raw: str) → bool` — raw escape hatch.

### Live data reads

The reader thread parses each `data,…` line into a `DataLog` and appends it
to a bounded ring buffer per channel (`deque(maxlen=4096)`).

- `has_new_data(ch) → bool` — set whenever a new sample has arrived since
  the last consume.
- `get_latest_sample(ch) → DataLog | None` — returns the newest sample and
  clears the new-data flag. O(1); intended for the main consumer loop.
- `peek_latest_sample(ch) → DataLog | None` — same, but does not touch the
  flag. Safe from auxiliary threads.
- `get_channel_data(ch) → list[DataLog]` — full copy of the ring. Pay this
  cost only when you need the whole history (e.g. at sweep end).
- `clear_channel_data(ch)` — wipe the ring. Call before a fresh sweep so
  points from a prior run do not linger.

`DataLog` fields:

```python
channel_id: int
mode: int        # 0=SWEEP, 1=STEADY, 2=IDLE, 3=CALIBRATION, 4=ODR
voltage: float   # V
current: float   # µA
time_s: float    # seconds since firmware boot (NOT wall time)
```

### Sweep TXT logging

Automatic once `start_reading` is running: every `data,…` line with
`mode == 0` is written to the channel's open sweep file.

- `start_sweep_log(ch) → path` — auto-rotates by timestamp.
- `stop_sweep_log(ch)`
- `sweep_log_path(ch) → str`

> The `start_steady_log` / `write_steady_row` helpers are shaped around the
> GUI's meter-poll workflow (hard-coded `pcb_current_uA` + per-meter columns,
> row written only when the caller explicitly hands over a meter reading).
> For a pcb-only headless recording, ignore those helpers and stream
> `peek_latest_sample(ch)` yourself — see the steady example below.

### Callbacks

Optional. Both default to `None`.

- `vs.log_callback = fn(msg: str)` — every non-`data,` line from the
  firmware (boot banners, `ESP_LOGx`, `printf`, watchdog messages).
- `vs.calib_callback = fn(ch, r_1k, r_gain, dac_vref)` — fires when the
  firmware sends a `calib,…` line (in reply to `send_calibration`, and on
  every boot).

## Threads

All threads are daemon threads and live entirely inside `vs_controller` — no
external ticker is required.

1. **Reader thread** (`start_reading`) — parses `data,…` and `calib,…`;
   forwards everything else to `log_callback`; writes sweep rows.
2. **Heartbeat thread** (`connect`) — writes `hb` every 2 s. The firmware
   zeros every channel after 2.5 s without input. `connect` starts it,
   `disconnect` stops it.
3. **Reconnect thread** — appears only after a USB drop, polls `list_ports()`
   for the last-known device, and re-runs `connect` + `start_reading` when
   it reappears. Bounded by `_reconnect_timeout_s = 30 s`. Set
   `vs._auto_reconnect = False` before `disconnect()` to opt out.

A diagnostic file `vs_diag_<timestamp>.log` is opened in `output_dir` on each
`connect`. It captures every non-`data,` line and heartbeat scheduling gaps.
Line-buffered so it survives a killed process.

## Gotchas

- `connect` alone does not spawn the reader — you must call `start_reading`.
- The ring buffer is bounded at 4096 samples. Fine for typical sweeps
  (~3000 points at 1.5 V range / 1 mV step). Very fine sweeps overflow and
  the oldest samples drop off the head; the sweep TXT is unaffected because
  it is written straight to disk.
- `send_command` returns `False` silently if the port is not open. Check the
  return value or `is_connected` if you care.
- `DataLog.time_s` is a firmware clock, not epoch. Stamp `time.time()`
  yourself when you read if you need wall time.
- The class is thread-safe for the exposed operations. Do not reach into
  `_serial` or `_data_buffers` directly — use the accessors above.

## Example — steady

```python
import csv
import time
from vs_controller import VoltageSourceController

vs = VoltageSourceController()
vs.log_callback = lambda s: print("[esp]", s)

vs.connect(vs.guess_port(), 115200)
vs.start_reading(output_dir="output")

ch, applied_v = 0, 0.25

# Run indefinitely — "Inf" ignores the duration argument.
vs.send_steady(ch, applied_v, 0, "Inf")

# Bounded run (firmware auto-turns off after `duration` `unit`s):
# vs.send_steady(ch, applied_v, 300, "Sec")

path = f"output/pcb_ch{ch}_{int(time.time())}.txt"
with open(path, "w", newline="") as f:
    w = csv.writer(f, delimiter="\t")
    w.writerow(["wall_time", "voltage_V", "pcb_current_uA"])
    try:
        while vs.is_connected:
            s = vs.peek_latest_sample(ch)
            if s is not None:
                w.writerow([f"{time.time():.3f}",
                            f"{s.voltage:.6f}",
                            f"{s.current:.6f}"])
                f.flush()
            time.sleep(1.0)   # sampling cadence
    except KeyboardInterrupt:
        pass
    finally:
        vs.send_off(ch)
        vs.stop_reading()
        vs.disconnect()
```

## Example — sweep

```python
import time
from vs_controller import VoltageSourceController

vs = VoltageSourceController()
vs.log_callback = lambda s: print("[esp]", s)

vs.connect(vs.guess_port(), 115200)
vs.start_reading(output_dir="output")

ch = 0
vs.clear_channel_data(ch)            # discard any prior samples
vs.start_sweep_log(ch)               # per-channel TXT, auto-filled by the reader
vs.send_sweep(ch, range_v=1.0, step_mv=10)

try:
    # Wait for the firmware to report OFF (mode == 2) after finishing the sweep.
    while vs.is_connected:
        s = vs.peek_latest_sample(ch)
        if s is not None and s.mode >= 2:
            break
        time.sleep(0.1)

    data = vs.get_channel_data(ch)   # full sweep, in order
    print(f"sweep collected {len(data)} points → {vs.sweep_log_path(ch)}")
finally:
    vs.stop_sweep_log(ch)
    vs.send_off(ch)
    vs.stop_reading()
    vs.disconnect()
```
