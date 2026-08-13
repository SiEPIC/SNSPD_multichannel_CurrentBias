import csv
import os
import threading
import time
import datetime
from dataclasses import dataclass

import serial
import serial.tools.list_ports


@dataclass
class DataLog:
    channel_id: int
    mode: int        # ESP Mode enum: 0=SWEEP, 1=STEADY, 2=IDLE, 3=CALIBRATION, 4=ODR
    voltage: float   # V (%.3f)
    current: float   # uA (%.3f)
    time_s: float    # Seconds since firmware start


class VoltageSourceController:

    def __init__(self):
        self._serial: serial.Serial | None = None
        # Only serializes writes / connect / close. Reads happen in the reader
        # thread without holding this lock so writes never wait on readline().
        self._write_lock = threading.Lock()

        # Optional callback for raw serial lines that aren't "data,…" records.
        # Wired by the GUI so firmware printf/ESP_LOGx output is visible.
        self.log_callback = None

        # Optional callback invoked whenever the firmware sends a
        # "calib, channel_id, r_1k, r_gain, dac_vref" line — used by the GUI
        # to keep the calibration input fields in sync with what the ESP
        # actually has stored (including the "all-NaN echo" reply and
        # boot-time sends from every channel).
        # Signature:
        #     (channel_id: int, r_1k: float, r_gain: float,
        #      dac_vref: float) -> None
        self.calib_callback = None

        self._data_buffers: dict[int, list] = {i: [] for i in range(4)}
        self._data_lock = threading.Lock()
        self._new_data: dict[int, bool] = {i: False for i in range(4)}

        # Per-channel sweep TXTs (one file per sweep run, per channel).
        # There is no "master" log — only SWEEP mode is logged to disk here.
        self._sweep_dir: str = "output"
        self._sweep_files: dict[int, object] = {}
        self._sweep_writers: dict[int, object] = {}
        self._sweep_paths: dict[int, str] = {}
        self._sweep_lock = threading.Lock()

        # Per-channel STEADY TXT logs (opened by the GUI when the user starts
        # a steady run with a timer AND at least one meter connected).
        # One row is written per meter poll — dedicated column per assigned
        # meter — so the file is a proper time series against meter cadence.
        self._steady_files: dict[int, object] = {}
        self._steady_writers: dict[int, object] = {}
        self._steady_paths: dict[int, str] = {}
        self._steady_meter_indices: dict[int, list[int]] = {}
        # Wall-clock t0 of each open steady log; row timestamps are written
        # as elapsed seconds against this so the first row is 0.000.
        self._steady_t0: dict[int, float] = {}
        self._steady_lock = threading.Lock()

        self._reading = False
        self._reader_thread: threading.Thread | None = None

        # Auto-reconnect state: remember the last successful connection so we
        # can re-open it after a native-USB soft reset re-enumerates the port.
        self._last_port: str = ""
        self._last_baud: int = 115200
        self._auto_reconnect: bool = False
        self._reconnect_thread: threading.Thread | None = None
        self._reconnect_stop: threading.Event = threading.Event()
        self._reconnect_timeout_s: float = 30.0
        self._last_output_dir: str = "output"

        # Cable-watchdog heartbeat. Firmware zeros every channel if it doesn't
        # see any stdin input within 2.5 s (task_comms.cpp wdt_connection), so
        # we send a token-only line every 2 s while connected. Interval must
        # stay comfortably below the firmware threshold.
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_stop: threading.Event = threading.Event()
        self._heartbeat_interval_s: float = 2.0

    @property
    def is_connected(self) -> bool:
        s = self._serial
        return s is not None and s.is_open

    @property
    def connected_port(self) -> str:
        s = self._serial
        return s.port if (s is not None and s.is_open) else ""

    def active_sweep_channels(self) -> list[int]:
        """Return channels that currently have an open sweep CSV."""
        with self._sweep_lock:
            return sorted(self._sweep_paths.keys())

    @staticmethod
    def list_ports() -> list[tuple[str, str]]:
        """Return list of (device, description) for all serial ports.

        USB-looking devices are sorted first (by the same VID/keyword score as
        guess_port), so the ESP shows up at the top of the port dropdown.
        Non-USB ports fall through in alphabetical order.
        """
        ports = list(serial.tools.list_ports.comports())

        def score(p) -> int:
            s = 0
            if p.vid == 0x303A:
                s += 100
            if p.vid in (0x10C4, 0x1A86, 0x0403):
                s += 50
            desc = " ".join(str(x or "") for x in (p.description, p.manufacturer, p.product)).lower()
            for kw in ("cp210", "ch340", "ftdi", "esp", "silicon labs", "usb serial", "usb"):
                if kw in desc:
                    s += 10
                    break
            dev = (p.device or "").lower()
            if "usbserial" in dev or "usbmodem" in dev or "ttyusb" in dev:
                s += 5
            elif "ttyacm" in dev:
                s += 3
            return s

        ports_sorted = sorted(ports, key=lambda p: (-score(p), p.device or ""))
        return [(p.device, p.description) for p in ports_sorted]

    @staticmethod
    def guess_port() -> str:
        """Best-effort auto-pick for the ESP USB serial device.

        Scores each port by VID (Espressif, SiLabs, WCH, FTDI), keywords in
        description/manufacturer/product, and device-path hints (/dev/ttyUSB*,
        /dev/tty.usbserial*, /dev/tty.usbmodem*, /dev/ttyACM*). Returns the
        highest-scoring device, or "" if nothing looks USB-ish.
        """
        ports = list(serial.tools.list_ports.comports())
        if not ports:
            return ""

        keywords = ("CP210", "CH340", "FTDI", "ESP", "Silicon Labs", "USB Serial")

        def score(p) -> int:
            s = 0
            if p.vid == 0x303A:  # Espressif native USB
                s += 100
            if p.vid in (0x10C4, 0x1A86, 0x0403):  # SiLabs, WCH, FTDI
                s += 50
            desc = " ".join(str(x or "") for x in (p.description, p.manufacturer, p.product)).lower()
            for kw in keywords:
                if kw.lower() in desc:
                    s += 10
            dev = (p.device or "").lower()
            if "usbserial" in dev or "usbmodem" in dev or "ttyusb" in dev:
                s += 5
            elif "ttyacm" in dev:
                s += 3
            return s

        best = max(ports, key=lambda p: (score(p), p.device or ""))
        return best.device if score(best) > 0 else ""

    def connect(self, port: str, baud: int = 115200) -> bool:
        self._reconnect_stop.set()
        self.stop_reading()
        check_connect = False
        with self._write_lock:
            self._close_locked()
            try:
                self._serial = serial.Serial(port, baud, timeout=0.2)
                print(f"[VoltageSource] Connected to {port} at {baud} baud")
                self._last_port = port
                self._last_baud = baud
                self._auto_reconnect = True
                check_connect = True
            except serial.SerialException as e:
                print(f"[VoltageSource] Connect failed on {port}: {e}")
                self._serial = None
        if check_connect:
            self._start_heartbeat()
        return check_connect

    def disconnect(self):
        self._auto_reconnect = False
        self._reconnect_stop.set()
        self.stop_reading()
        with self._write_lock:
            self._close_locked()

    def _close_locked(self):
        self._stop_heartbeat()
        s = self._serial
        if s is not None and s.is_open:
            try:
                s.close()
            except Exception:
                pass
        self._serial = None
        print("[VoltageSource] Disconnected")

    def _start_heartbeat(self):
        """Spawn the background thread that keeps the firmware USB cable-watchdog fed."""
        self._stop_heartbeat()
        stop = threading.Event()
        self._heartbeat_stop = stop

        def _loop():
            while not stop.is_set():
                if stop.wait(self._heartbeat_interval_s):
                    return
                if not self.is_connected:
                    continue
                try:
                    self.send_command("hb")
                except Exception:
                    pass

        t = threading.Thread(target=_loop, daemon=True, name="vs-heartbeat")
        self._heartbeat_thread = t
        t.start()

    def _stop_heartbeat(self):
        self._heartbeat_stop.set()
        self._heartbeat_thread = None

    def send_command(self, raw: str) -> bool:
        with self._write_lock:
            s = self._serial
            if s is None or not s.is_open:
                return False
            try:
                s.write((raw.strip() + "\0").encode())
                return True
            except serial.SerialException as e:
                print(f"[VoltageSource] Send failed: {e}")
                return False

    def send_off(self, channel_id: int):
        self.send_command(f"{channel_id},OFF")

    def send_steady(self, channel_id: int, voltage: float, duration: float, time_unit: str):
        self.send_command(f"{channel_id},STEADY,{voltage},{duration},{time_unit}")

    def send_sweep(self, channel_id: int, range_v: float, step_size: float):
        self.send_command(f"{channel_id},SWEEP,{range_v},{step_size}")

    def send_calibration(self, channel_id: int, r_1k: float, r_gain: float,
                         dac_vref: float):
        """Firmware persists these values in NVS."""
        self.send_command(
            f"{channel_id},CALIBRATION,{r_1k},{r_gain},{dac_vref}"
        )

    def send_odr(self, channel_id: int, rate: float):
        """Send ADC output-data-rate setting for a channel (rate in SPS)."""
        self.send_command(f"{channel_id},ODR,{rate}")

    def start_sweep_log(self, channel_id: int) -> str:
        """Open a fresh per-channel TXT for an incoming sweep run.

        Closes any previous sweep file for this channel. Returns the new path
        or "" on failure.
        """
        os.makedirs(self._sweep_dir, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self._sweep_dir, f"vs_ch{channel_id}_sweep_{timestamp}.txt")
        try:
            f = open(path, "w", newline="")
            w = csv.writer(f, delimiter="\t")
            w.writerow(["voltage_V", "current_uA", "time_s"])
            f.flush()
        except Exception as e:
            print(f"[VoltageSource] Failed to open sweep log Ch{channel_id}: {e}")
            return ""

        with self._sweep_lock:
            self._close_sweep_locked(channel_id)
            self._sweep_files[channel_id] = f
            self._sweep_writers[channel_id] = w
            self._sweep_paths[channel_id] = path
        print(f"[VoltageSource] Ch{channel_id} sweep log → {path}")
        return path

    def stop_sweep_log(self, channel_id: int):
        """Close the sweep TXT for this channel if one is open."""
        with self._sweep_lock:
            self._close_sweep_locked(channel_id)

    def _close_sweep_locked(self, channel_id: int):
        f = self._sweep_files.pop(channel_id, None)
        self._sweep_writers.pop(channel_id, None)
        self._sweep_paths.pop(channel_id, None)
        if f is not None:
            try:
                f.close()
            except Exception:
                pass

    def sweep_log_path(self, channel_id: int) -> str:
        with self._sweep_lock:
            return self._sweep_paths.get(channel_id, "")

    # ---- STEADY txt log ---------------------------------------------------
    def start_steady_log(self, channel_id: int, applied_voltage: float,
                          duration_s: float | None,
                          meter_indices: list[int]) -> str:
        """Open a per-channel TXT log for a steady run.

        Columns (tab-separated):
            time_s, voltage_V, pcb_current_uA,
            meter{idx0}_current_uA, meter{idx1}_current_uA, ...

        ``time_s`` is elapsed seconds since the log opened (first row = 0.0).

        One row is written per meter poll (via ``write_steady_row``). The
        meter that fired gets its column populated; the other meter columns
        on that row are left blank.
        Returns "" on failure.
        """
        os.makedirs(self._sweep_dir, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self._sweep_dir,
                            f"vs_ch{channel_id}_steady_{timestamp}.txt")
        indices = list(meter_indices)
        try:
            f = open(path, "w", newline="")
            dur_str = "inf" if duration_s is None else f"{duration_s:.1f}s"
            f.write(f"# Ch {channel_id} steady log\n")
            f.write(f"# applied_voltage_V\t{applied_voltage:.6f}\n")
            f.write(f"# duration\t{dur_str}\n")
            f.write(f"# meters\t{','.join(str(i) for i in indices) or 'none'}\n")
            w = csv.writer(f, delimiter="\t")
            header = ["time_s", "voltage_V", "pcb_current_uA"]
            for idx in indices:
                header.append(f"meter{idx}_current_uA")
            w.writerow(header)
            f.flush()
        except Exception as e:
            print(f"[VoltageSource] Failed to open steady log Ch{channel_id}: {e}")
            return ""

        with self._steady_lock:
            self._close_steady_locked(channel_id)
            self._steady_files[channel_id] = f
            self._steady_writers[channel_id] = w
            self._steady_paths[channel_id] = path
            self._steady_meter_indices[channel_id] = indices
            self._steady_t0[channel_id] = time.time()
        print(f"[VoltageSource] Ch{channel_id} steady log → {path}")
        return path

    def stop_steady_log(self, channel_id: int):
        with self._steady_lock:
            self._close_steady_locked(channel_id)

    def _close_steady_locked(self, channel_id: int):
        f = self._steady_files.pop(channel_id, None)
        self._steady_writers.pop(channel_id, None)
        self._steady_paths.pop(channel_id, None)
        self._steady_meter_indices.pop(channel_id, None)
        self._steady_t0.pop(channel_id, None)
        if f is not None:
            try:
                f.close()
            except Exception:
                pass

    def steady_log_path(self, channel_id: int) -> str:
        with self._steady_lock:
            return self._steady_paths.get(channel_id, "")

    def write_steady_row(self, channel_id: int, timestamp: float,
                          voltage_V: float, pcb_current_uA: float | None,
                          meter_idx: int, meter_current_uA: float):
        """Append one meter-poll row to the channel's steady log if open.

        ``timestamp`` is a wall-clock epoch value; the row's ``time_s``
        column is written as elapsed seconds since the log opened, so the
        first row is 0.000 and subsequent rows step forward in seconds.

        Only the polling meter's column is populated on this row; the other
        meters' columns are left blank so the reader can tell which meter
        the sample came from without parsing an extra column.
        """
        with self._steady_lock:
            w = self._steady_writers.get(channel_id)
            f = self._steady_files.get(channel_id)
            indices = self._steady_meter_indices.get(channel_id, [])
            t0 = self._steady_t0.get(channel_id, timestamp)
            if w is None or f is None:
                return
            try:
                elapsed = max(0.0, timestamp - t0)
                row = [
                    f"{elapsed:.3f}",
                    f"{voltage_V:.6f}",
                    "" if pcb_current_uA is None else f"{pcb_current_uA:.6f}",
                ]
                for idx in indices:
                    row.append(f"{meter_current_uA:.6f}" if idx == meter_idx else "")
                w.writerow(row)
                f.flush()
            except Exception:
                pass

    def start_reading(self, output_dir: str = "output"):
        """Start the background reader thread. Only SWEEP mode logs to disk."""
        if self._reading:
            return

        os.makedirs(output_dir, exist_ok=True)
        self._sweep_dir = output_dir
        self._last_output_dir = output_dir

        self._reading = True
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        print("[VoltageSource] Started reading (sweep-CSV only)")

    def stop_reading(self):
        """Stop the reader thread and close any open sweep CSVs."""
        self._reading = False
        t = self._reader_thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._reader_thread = None

        with self._sweep_lock:
            for ch in list(self._sweep_files.keys()):
                self._close_sweep_locked(ch)
        with self._steady_lock:
            for ch in list(self._steady_files.keys()):
                self._close_steady_locked(ch)
        print("[VoltageSource] Stopped reading")

    def _handle_lost_connection(self, reason: str):
        """Close the port and clear state after the USB device disappears.

        Called from the reader thread so is_connected flips to False and the
        GUI's idle loop picks up the change immediately. Safe to call while
        holding no locks; acquires _write_lock internally.
        """
        self._reading = False
        with self._write_lock:
            self._close_locked()
        msg = f"[VoltageSource] Connection lost: {reason}"
        print(msg)
        cb = self.log_callback
        if cb is not None:
            try:
                cb(msg)
            except Exception:
                pass
        if self._auto_reconnect and self._last_port:
            self._start_reconnect_watcher()

    def _start_reconnect_watcher(self):
        """Spawn (or reuse) the background thread that waits for the port to
        reappear and then re-opens it."""
        t = self._reconnect_thread
        if t is not None and t.is_alive():
            return
        self._reconnect_stop = threading.Event()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True
        )
        self._reconnect_thread.start()

    def _reconnect_loop(self):
        """Poll list_ports until the last-known device reappears; then reconnect.

        Runs for at most _reconnect_timeout_s. Aborts if the user issues an
        explicit connect/disconnect (which sets _reconnect_stop).
        """
        port = self._last_port
        baud = self._last_baud
        stop = self._reconnect_stop
        deadline = time.time() + self._reconnect_timeout_s
        cb = self.log_callback
        if cb is not None:
            try:
                cb(f"[VoltageSource] Waiting for {port} to reappear…")
            except Exception:
                pass

        while not stop.is_set() and time.time() < deadline:
            try:
                active = {p.device for p in serial.tools.list_ports.comports()}
            except Exception:
                active = set()
            if port in active:
                if stop.wait(0.3):
                    return
                if self.connect(port, baud):
                    self.start_reading(self._last_output_dir)
                    msg = f"[VoltageSource] Auto-reconnected to {port}"
                    print(msg)
                    if cb is not None:
                        try:
                            cb(msg)
                        except Exception:
                            pass
                    return
            if stop.wait(0.5):
                return

        if not stop.is_set() and cb is not None:
            try:
                cb(f"[VoltageSource] Gave up auto-reconnect after {int(self._reconnect_timeout_s)}s")
            except Exception:
                pass

    def _read_loop(self):
        """Background loop: read lines, parse data, buffer and log to CSV."""
        s0 = self._serial
        port_name = s0.port if s0 is not None else ""
        last_port_check = time.time()

        while self._reading:
            s = self._serial
            if s is None or not s.is_open:
                break
            try:
                raw = s.readline()
            except OSError as e:
                self._handle_lost_connection(f"read error: {e}")
                break
            except Exception as e:
                print(f"[VoltageSource] Reader read error: {e}")
                time.sleep(0.1)
                continue

            now = time.time()
            if port_name and now - last_port_check >= 1.0:
                last_port_check = now
                try:
                    active = {p.device for p in serial.tools.list_ports.comports()}
                except Exception:
                    active = None
                if active is not None and port_name not in active:
                    self._handle_lost_connection(f"USB device {port_name} unplugged")
                    break

            if not raw:
                continue
            line = raw.decode(errors="replace").strip()
            if line.startswith("calib,"):
                # Firmware echo: "calib, r_1k, r_gain, dac_vref"
                self._handle_calib_line(line)
                continue
            if not line.startswith("data,"):
                if line and self.log_callback is not None:
                    try:
                        self.log_callback(f"[ESP] {line}")
                    except Exception:
                        pass
                continue
            parts = line.split(",")
            if len(parts) < 6:
                continue
            try:
                channel_id = int(parts[1])
                mode = int(parts[2])
                voltage = float(parts[3])
                current = float(parts[4])
                time_s = float(parts[5])
            except (ValueError, IndexError):
                continue

            entry = DataLog(
                channel_id=channel_id,
                mode=mode,
                voltage=voltage,
                current=current,
                time_s=time_s,
            )
            with self._data_lock:
                if channel_id in self._data_buffers:
                    self._data_buffers[channel_id].append(entry)
                    self._new_data[channel_id] = True

            # Per-channel sweep log (mode 0 = SWEEP). Steady log is written
            # only when a meter polls (see GUI meter poll worker), so nothing
            # to log here for mode 1.
            if mode == 0:
                with self._sweep_lock:
                    w = self._sweep_writers.get(channel_id)
                    f = self._sweep_files.get(channel_id)
                    if w is not None and f is not None:
                        try:
                            w.writerow([voltage, current, time_s])
                            f.flush()
                        except Exception:
                            pass

    def _handle_calib_line(self, line: str):
        """Parse a firmware calibration echo and hand it to the GUI callback.

        Line format: ``calib, %d, %.4f, %.4f, %.5f``
        (channel_id, r_1k, r_gain, dac_vref).
        The firmware sends this on boot for every channel, after a successful
        set_calibration(), and when the GUI requests the stored values by
        writing all-NaN.
        """
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            return
        try:
            channel_id = int(parts[1])
            r_1k = float(parts[2])
            r_gain = float(parts[3])
            dac_vref = float(parts[4])
        except ValueError:
            return
        cb = self.calib_callback
        if cb is not None:
            try:
                cb(channel_id, r_1k, r_gain, dac_vref)
            except Exception:
                pass

    def has_new_data(self, channel_id: int) -> bool:
        """Return True if new data has arrived for this channel since last get."""
        return self._new_data.get(channel_id, False)

    def get_channel_data(self, channel_id: int) -> list:
        """Return a copy of the data buffer for a channel and clear the new-data flag."""
        with self._data_lock:
            self._new_data[channel_id] = False
            return list(self._data_buffers[channel_id])

    def clear_channel_data(self, channel_id: int):
        """Discard all buffered data for a channel."""
        with self._data_lock:
            self._data_buffers[channel_id].clear()
