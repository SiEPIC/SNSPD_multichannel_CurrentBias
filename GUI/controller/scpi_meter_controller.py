"""Cross-platform SCPI meter driver over VISA.

Works with any single-channel SCPI measurement instrument reachable via
NI-VISA — DMMs (DMM6500, DMM7510, 2100, 2110, 34461A, DM3068, …) as well
as SMUs (2400, 2450, 2600) used purely for readback (never sourcing).

Requires:
    pip install pyvisa
    plus the NI-VISA runtime installed system-wide:
        Linux/Mac/Windows: https://www.ni.com/visa
"""

import threading

import pyvisa
from pyvisa.errors import VisaIOError


class ScpiMeterController:
    """Single-channel, measurement-only SCPI instrument driver.

    Mirrors the connect/disconnect + log_callback pattern of
    VoltageSourceController so GUI wiring feels the same.

    Nothing here sources — safe to point at an SMU for readback without
    ever enabling its output.
    """

    def __init__(self):
        self._inst = None
        self._resource: str = ""
        self.idn: str = ""

        # Serializes every write/read pair so background workers can't
        # interleave requests and steal each other's responses. Also protects
        # _inst against disconnect() closing it mid-operation.
        self._io_lock = threading.Lock()

        # GUI attaches this to surface errors in its log widget.
        # Matches VoltageSourceController.log_callback.
        self.log_callback = None

        # Per-request timeout in ms. Bump this before connect() if you use
        # high-NPLC readings that take longer than 2 s.
        self._timeout_ms: int = 2000

    @property
    def is_connected(self) -> bool:
        return self._inst is not None

    @property
    def connected_resource(self) -> str:
        if self._inst is not None:
            return self._resource
        else:
            return ""


    @staticmethod
    def list_devices() -> list[str]:
        """Return all USB VISA resource strings currently visible."""
        try:
            rm = pyvisa.ResourceManager()
            return [r for r in rm.list_resources() if r.startswith("USB")]
        except Exception:
            return []

    def _log(self, msg: str):
        print(msg)
        cb = self.log_callback
        if cb is not None:
            try:
                cb(msg)
            except Exception:
                pass

    def connect(self, resource: str) -> bool:
        """Open the VISA resource and confirm it responds to *IDN?."""
        self.disconnect()
        try:
            rm = pyvisa.ResourceManager()
            inst = rm.open_resource(resource)
            inst.timeout = self._timeout_ms
        except VisaIOError as e:
            self._log(f"[Meter] Open {resource} failed: {e}")
            return False
        except Exception as e:
            self._log(f"[Meter] Open {resource} failed: {e}")
            return False

        with self._io_lock:
            self._inst = inst
            self._resource = resource

        try:
            self.idn = self._query("*IDN?")
        except Exception as e:
            self._log(f"[Meter] IDN query failed: {e}")
            self.disconnect()
            return False

        self._log(f"[Meter] Connected {resource}: {self.idn}")
        return True

    def disconnect(self):
        """Close the VISA resource. Safe to call when already disconnected."""
        with self._io_lock:
            inst = self._inst
            self._inst = None
            self._resource = ""
            self.idn = ""
        if inst is not None:
            try:
                inst.close()
            except Exception:
                pass
            self._log("[Meter] Disconnected")

    # -- low-level SCPI I/O (all lock-protected) --

    def _query(self, cmd: str) -> str:
        """Atomic write+read under the io lock."""
        with self._io_lock:
            if self._inst is None:
                raise RuntimeError("Meter not connected")
            return self._inst.query(cmd).strip()

    def write(self, cmd: str) -> bool:
        """Send a SCPI command with no response. Returns True on success."""
        try:
            with self._io_lock:
                if self._inst is None:
                    raise RuntimeError("Meter not connected")
                self._inst.write(cmd)
            return True
        except VisaIOError as e:
            self._log(f"[Meter] write failed: {e}")
            self._handle_lost_connection(e)
            return False

    def query(self, cmd: str) -> str:
        """Public wrapper around _query for callers needing arbitrary SCPI."""
        return self._query(cmd)

    # -- one-shot measurements (slow: ~100-500 ms each because they reconfigure) --

    def measure_voltage(self) -> float:
        """DC voltage in volts. Returns NaN if the read fails."""
        return self._safe_measure(":MEAS:VOLT?")

    def measure_current(self) -> float:
        """DC current in amps. Returns NaN if the read fails."""
        return self._safe_measure(":MEAS:CURR?")

    # -- fast repeated sampling: configure once, read many --

    def configure_voltage_dc(self, v_range: float | None = None,
                              nplc: float | None = None) -> bool:
        """Set up the front end for repeated DC voltage sampling via read()."""
        if not self.write(":SENS:FUNC 'VOLT:DC'"):
            return False
        if v_range is None:
            self.write(":SENS:VOLT:DC:RANG:AUTO ON")
        else:
            self.write(f":SENS:VOLT:DC:RANG {v_range}")
        if nplc is not None:
            self.write(f":SENS:VOLT:DC:NPLC {nplc}")
        return True

    def configure_current_dc(self, i_range: float | None = None,
                              nplc: float | None = None) -> bool:
        """Set up the front end for repeated DC current sampling via read()."""
        if not self.write(":SENS:FUNC 'CURR:DC'"):
            return False
        if i_range is None:
            self.write(":SENS:CURR:DC:RANG:AUTO ON")
        else:
            self.write(f":SENS:CURR:DC:RANG {i_range}")
        if nplc is not None:
            self.write(f":SENS:CURR:DC:NPLC {nplc}")
        return True

    def read(self) -> float:
        """One sample of whatever function was configured last. Much faster
        than measure_voltage/measure_current in a tight loop."""
        return self._safe_measure(":READ?")

    # -- internal helpers --

    def _safe_measure(self, scpi: str) -> float:
        try:
            return float(self._query(scpi))
        except VisaIOError as e:
            self._log(f"[Meter] {scpi} failed: {e}")
            self._handle_lost_connection(e)
            return float("nan")
        except ValueError as e:
            self._log(f"[Meter] {scpi} returned non-numeric: {e}")
            return float("nan")

    def _handle_lost_connection(self, exc: Exception):
        """VISA errors on read/write usually mean the USB device disappeared
        or the driver reset the pipe. Close so is_connected reflects reality."""
        self._log("[Meter] Connection lost — closing handle")
        self.disconnect()
