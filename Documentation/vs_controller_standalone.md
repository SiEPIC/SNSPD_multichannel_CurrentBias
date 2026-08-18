# Using `vs_controller` without the GUI

`vs_controller.py` (at `GUI/src/gui/controller/vs_controller.py`) is
self-contained: its only runtime dependency is `pyserial`. It can be imported
and driven from any Python script — the GUI is just one consumer of it.

## Importing it

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

## Minimum happy-path

```python
vs = VoltageSourceController()

port = vs.guess_port() or "/dev/ttyUSB0"   # or hard-code
vs.connect(port, baud=115200)              # spawns the heartbeat thread
vs.start_reading(output_dir="output")      # spawns the reader thread

vs.send_steady(channel_id=0, voltage=0.25, duration=300, time_unit="Sec")
# … work …
vs.send_off(0)

vs.stop_reading()   # closes any open sweep/steady logs
vs.disconnect()     # stops heartbeat, closes port
```

Order matters: **connect → start_reading → send_… → stop_reading → disconnect**.
Skip `start_reading` and no `data,…` lines are parsed. Skip `connect` (i.e. open
a `serial.Serial` yourself) and the firmware's 2.5 s cable watchdog will zero
every channel because no heartbeat is running.

## Public API surface

### Static helpers

- `VoltageSourceController.list_ports() → list[tuple[device, description]]` —
  USB-looking devices sorted first (by VID + keyword score).
- `VoltageSourceController.guess_port() → str` — best pick, or `""` if nothing
  looks USB-ish.

### Connection state

- `is_connected: bool` (property)
- `connected_port: str` (property)
- `active_sweep_channels() → list[int]`

### Command senders

All are fire-and-forget writes terminated with `\0`. Each returns `False`
silently if the port is not open.

- `send_off(ch)`
- `send_steady(ch, voltage, duration, time_unit)` — `time_unit` is one of the
  strings the firmware accepts (e.g. `"Sec"`, `"Min"`, `"Hour"`, `"Inf"`).
- `send_sweep(ch, range_v, step_mv)`
- `send_calibration(ch, r_1k, r_gain, dac_vref)` — persisted in NVS on the ESP.
- `send_odr(ch, sps)`
- `send_command(raw: str) → bool` — raw escape hatch.

### Live data reads

The reader thread parses each incoming `data,…` line into a `DataLog` and
appends it to a bounded ring buffer per channel (`deque(maxlen=4096)`).

- `has_new_data(ch) → bool` — flag set since last `get_latest_sample` /
  `get_channel_data`.
- `get_latest_sample(ch) → DataLog | None` — returns the newest sample and
  clears the new-data flag. Cheap (O(1)); designed for tight consumer loops.
- `peek_latest_sample(ch) → DataLog | None` — same, but does **not** touch the
  flag. Use this from threads other than your main consumer.
- `get_channel_data(ch) → list[DataLog]` — full copy of the ring. Only pay this
  cost once per sweep end.
- `clear_channel_data(ch)` — wipe the ring. Call before a fresh sweep so old
  points do not linger.

`DataLog` is a dataclass with:

```python
channel_id: int
mode: int        # 0=SWEEP, 1=STEADY, 2=IDLE, 3=CALIBRATION, 4=ODR
voltage: float   # V
current: float   # µA
time_s: float    # seconds since firmware boot (NOT wall time)
```

### Optional TXT logging

Sweep logging is automatic once `start_reading` is running: any `data,…` line
with `mode == 0` is written to that channel's open sweep file.

- `start_sweep_log(ch) → path` — auto-rotates by timestamp.
- `stop_sweep_log(ch)`
- `sweep_log_path(ch) → str`

Steady logging is opt-in and manually driven. The reader does **not** write
steady rows for you — you call `write_steady_row` from your metering loop.

- `start_steady_log(ch, applied_voltage, duration_s, meter_indices) → path` —
  writes header block + column row. Columns: `time_s`, `voltage_V`,
  `pcb_current_uA`, then one `meter{idx}_current_uA` per meter you passed.
- `write_steady_row(ch, timestamp, voltage_V, pcb_current_uA, meter_idx,
  meter_current_uA)` — no-op if `start_steady_log` was not called first.
  `timestamp` is wall-clock epoch; the file stores elapsed seconds since the
  first row so row 0 is exactly `0.000`.
- `stop_steady_log(ch)`
- `steady_log_path(ch) → str`

### Callbacks

Optional. Both default to `None`.

- `vs.log_callback = fn(msg: str)` — every non-`data,` line from the firmware
  (boot banners, `ESP_LOGx`, `printf`, watchdog messages) is forwarded here.
- `vs.calib_callback = fn(ch, r_1k, r_gain, dac_vref)` — fires when the
  firmware sends a `calib,…` line (in reply to `send_calibration`, and on
  every boot).

## Threads it spawns (all daemon)

1. **Reader thread** (`start_reading`) — parses `data,…`, `calib,…`; passes
   everything else to `log_callback`; writes sweep rows.
2. **Heartbeat thread** (`connect`) — writes `hb` every 2 s. Non-optional; the
   firmware zeros all channels if it goes 2.5 s without input.
3. **Reconnect thread** — appears only after a USB drop, polls
   `list_ports()` for the last-known device, and re-runs `connect` +
   `start_reading` when it reappears. Bounded by `_reconnect_timeout_s = 30 s`.
   Set `vs._auto_reconnect = False` before `disconnect()` to opt out.

A diagnostic file `vs_diag_<timestamp>.log` is also opened in `output_dir` on
each `connect`. It captures every non-`data,` line plus heartbeat scheduling
gaps. Line-buffered — survives a killed process. Safe to ignore.

## Gotchas

- **Call `start_reading` after `connect`.** `connect` alone does not spawn the
  reader. Without it, `has_new_data` is always `False`.
- **Ring buffer is bounded at 4096 samples.** Fine for typical sweeps
  (~3000 points at 1.5 V range / 1 mV step). Very fine sweeps overflow → old
  points fall off the head. Sweep TXT is unaffected (written straight to disk).
- **`send_command` returns `False` silently** if the port is not open — check
  it or check `is_connected` first if you care.
- **`time_s` on a `DataLog` is a firmware clock**, not epoch. Stamp
  `time.time()` yourself when you read if you need wall time.
- **`write_steady_row` is a no-op** if `start_steady_log(ch, …)` was not
  called first for that channel.
- **Concurrency:** the class is thread-safe for the exposed operations. Do
  not touch `_serial` or `_data_buffers` directly from your own code — use
  the accessors above.

## Full example: long steady run with a meter, no GUI

```python
import time
from vs_controller import VoltageSourceController

vs = VoltageSourceController()
vs.log_callback = lambda s: print("[esp]", s)

vs.connect(vs.guess_port(), 115200)
vs.start_reading(output_dir="output")

ch, applied_v = 0, 0.25
vs.start_steady_log(ch, applied_v, duration_s=3600, meter_indices=[0])
vs.send_steady(ch, applied_v, 3600, "Sec")

try:
    t_end = time.time() + 3600
    while time.time() < t_end and vs.is_connected:
        time.sleep(30)                            # your meter cadence
        i_meter_uA = your_meter.measure_current() * 1e6
        latest = vs.peek_latest_sample(ch)
        pcb_uA = latest.current if latest else None
        vs.write_steady_row(ch, time.time(), applied_v, pcb_uA, 0, i_meter_uA)
finally:
    vs.send_off(ch)
    vs.stop_steady_log(ch)
    vs.stop_reading()
    vs.disconnect()
```
