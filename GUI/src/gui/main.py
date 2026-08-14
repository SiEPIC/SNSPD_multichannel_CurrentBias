"""Voltage Source GUI — 2 tabs (Ctrl, Calib), N-meter comparison, sweep plot popup."""

import atexit
import base64
import csv
import datetime
import io
import logging
import os
import platform
import signal
import sys
import threading
import time
import uuid

import webview
from remi import App, start
import remi.gui as gui

from .layout.lib_gui import (
    StyledButton,
    StyledCheckBox,
    StyledContainer,
    StyledDropDown,
    StyledLabel,
    StyledTextInput,
)
from .layout.window_config import get_window_config
from .controller.scpi_meter_controller import ScpiMeterController
from .controller.vs_controller import VoltageSourceController

logging.getLogger("remi").setLevel(logging.WARNING)


class _StaleWidgetFilter(logging.Filter):
    """Silence remi's 'error parsing websocket' KeyErrors.

    A browser tab held over from a previous Python process still has
    widget IDs cached in JS; the first event it fires after the WS
    reconnects hits runtimeInstances[stale_id] and raises KeyError before
    our boot-ID reload can take effect. That log message + traceback is
    pure noise — the reload handles it a few ms later.
    """

    def filter(self, record):
        if "error parsing websocket" not in record.getMessage():
            return True
        exc_info = record.exc_info
        if exc_info is None:
            return True
        exc_type = exc_info[0]
        return exc_type is not KeyError


logging.getLogger("remi.server.ws").addFilter(_StaleWidgetFilter())

# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------
_W, _H = 1430, 1060
_TAB_LABELS = ["Ctrl", "Calib"]
_TAB_W, _TAB_H, _TAB_GAP = 75, 50, 6
_TAB_X = _W - _TAB_W - 10
_TAB_Y0 = 60
_PANEL_Y, _PANEL_H = 20, 1040
_CONTENT_RIGHT_EDGE = _TAB_X - 15

_CH_COLORS = {0: "#007BFF", 1: "#28a745", 2: "#dc3545", 3: "#fd7e14"}
_METER_COLORS = ["#000000", "#6f42c1", "#20c997", "#e83e8c"]

_V_MIN, _V_MAX = -5.0, 5.0
_SWEEP_RANGE_MAX = 1.5
_DOT_OFF, _DOT_ON = "#dc3545", "#28a745"

_MODE_LABELS = ["OFF", "STEADY", "SWEEP"]
_MODE_ACTIVE = ("#28a745", "#1e7e34")
_MODE_OFF_ACTIVE = ("#495057", "#343a40")
_MODE_UNSEL = ("#adb5bd", "#868e96")

_ODR_OPTIONS = ["1.25", "2.5", "5", "10", "16", "20", "49", "59", "100", "200"]
_ODR_DEFAULT = "10"

_CAL_R1K_DEF = "1000.0"
_CAL_RGAIN_DEF = "402.0"
_CAL_VREF_DEF = "2.5"

_METER_POLL_S = 1.0          # seconds between meter samples (initial default)
_METER_POLL_MIN_S = 0.05     # safety floor to avoid saturating VISA / meter
_ROLLING_WINDOW_S = 60.0     # calib plot look-back window
_PCB_LOG_INTERVAL = 0.05     # min seconds between PCB samples pushed to rolling buffer
_CALIB_RENDER_HZ = 1.0

_METER_OUTPUT_DIR = "output"

# Unique per Python process. Rendered into every session and compared in
# the browser's localStorage — if a browser tab is still open from a
# previous Python process it holds stale widget IDs, so on WebSocket
# reconnect we force it to reload rather than let event callbacks fail
# with KeyError on runtimeInstances lookups.
_BOOT_ID = uuid.uuid4().hex


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Log buffers (module-level so both worker threads and GUI can write)
# ---------------------------------------------------------------------------
_log_lines: list[str] = []
_log_seq = 0
_LOG_MAX = 2000

# Calib tab shows a filtered subset (meter connect + recalibration only).
_calib_log_lines: list[str] = []
_calib_log_seq = 0
_CALIB_LOG_MAX = 400


def _log(msg: str, *, calib: bool = False):
    global _log_seq, _calib_log_seq
    _log_lines.append(msg)
    if len(_log_lines) > _LOG_MAX:
        del _log_lines[:len(_log_lines) - _LOG_MAX]
    _log_seq += 1
    if calib:
        _calib_log_lines.append(msg)
        if len(_calib_log_lines) > _CALIB_LOG_MAX:
            del _calib_log_lines[:len(_calib_log_lines) - _CALIB_LOG_MAX]
        _calib_log_seq += 1
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Module singletons — shared across all remi client sessions
# ---------------------------------------------------------------------------
_vs = VoltageSourceController()
_vs.log_callback = _log

_meters: list[ScpiMeterController | None] = [None] * 4


def _meter_connected(idx: int) -> bool:
    m = _meters[idx]
    return m is not None and m.is_connected


# ===========================================================================
# App
# ===========================================================================
class VoltageSourceApp(App):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------
    def main(self):
        self._init_state()
        # Route firmware calib echoes back into the GUI so the input fields
        # stay in sync with what the ESP has actually stored (including the
        # all-NaN "read stored values" reply).
        _vs.calib_callback = self._on_calib_received
        root = self._build_ui()
        # Fire the boot-ID reload check IMMEDIATELY (before build_ui returns
        # to remi), not on the first idle() tick — otherwise a browser tab
        # from a previous Python process gets to send a batch of stale-widget
        # events before the reload can fire.
        #
        # sessionStorage acts as a fuse: it persists across location.reload()
        # but resets when the tab is closed. That way we can guarantee at
        # most ONE reload per tab per boot-ID even if localStorage misbehaves
        # (private mode, strict-privacy settings, setItem not flushing
        # before reload, etc.), so we can never end up in a reload loop.
        try:
            self.execute_javascript(
                "(function(id){"
                "var k='remi_boot_id',fk='remi_boot_reloaded',s=null;"
                "try{s=localStorage.getItem(k);}catch(e){return;}"
                "if(s===id)return;"
                "try{localStorage.setItem(k,id);}catch(e){}"
                "if(s===null)return;"
                "try{"
                "if(sessionStorage.getItem(fk)===id)return;"
                "sessionStorage.setItem(fk,id);"
                "}catch(e){return;}"
                "location.reload();"
                f"}})('{_BOOT_ID}');")
            self._boot_id_checked = True
        except Exception:
            pass
        return root

    def _init_state(self):
        # Tab / panel
        self._panels: list = []
        self._tab_btns: list = []
        self._active_tab = 0

        # ESP connection widgets
        self._port_dd = None
        self._baud_dd = None
        self._status_label = None
        self._connect_btn = None
        self._was_connected = False

        # Per-channel widgets
        self._status_dots = [None] * 4
        self._mode_btns: list[list] = [[] for _ in range(4)]
        self._mode_indicator_lbls = [None] * 4
        self._odr_dds = [None] * 4

        self._steady_containers = [None] * 4
        self._sweep_containers = [None] * 4
        self._voltage_inputs = [None] * 4
        self._range_inputs = [None] * 4
        self._step_inputs = [None] * 4
        self._timer_checkboxes = [None] * 4
        self._duration_inputs = [None] * 4
        self._time_unit_dds = [None] * 4

        self._readout_lbls = [None] * 4        # live current display
        self._sweep_link_lbls = [None] * 4     # "View Plot" link (per channel)
        self._sweep_dot_lbls = [None] * 4      # blue "new sweep" indicator
        self._apply_btns = [None] * 4

        self._selected_modes = ["OFF"] * 4     # what user picked in mode buttons
        self._active_modes = ["OFF"] * 4       # what firmware last reported
        self._applied_voltages = [0.0] * 4
        self._sweep_active = [False] * 4
        # Sweep plot data per channel: (voltages, currents) — used by the
        # Plotly popup so zoom operates on the data, not a rasterized image.
        self._sweep_plot_data: dict[int, tuple[list[float], list[float]]] = {}

        # Calib tab widgets
        self._cal_r1k_inputs = [None] * 4
        self._cal_rgain_inputs = [None] * 4
        self._cal_vref_inputs = [None] * 4
        self._recal_btns = [None] * 4
        self._fetch_calib_btns = [None] * 4

        self._meter_count_dd = None
        self._meter_rows_container = None
        self._meter_port_dds = [None] * 4
        self._meter_status_lbls = [None] * 4
        self._meter_connect_btns = [None] * 4
        self._meter_channel_dds = [None] * 4
        self._meter_poll_inputs = [None] * 4
        self._meter_count = 0
        self._meter_channel = [0, 1, 2, 3]     # default: meter i → channel i
        self._meter_was_connected = [False] * 4
        self._meter_poll_threads: list[threading.Thread | None] = [None] * 4
        self._meter_poll_stops: list[threading.Event | None] = [None] * 4
        # Live-editable per-meter polling interval — the meter i worker
        # re-reads self._meter_poll_s[i] each loop so edits take immediate
        # effect without restarting the thread.
        self._meter_poll_s: list[float] = [_METER_POLL_S] * 4

        self._calib_plot_imgs = [None] * 4
        self._calib_no_data_lbls = [None] * 4
        self._calib_readout_lbls = [None] * 4
        self._last_calib_render = [0.0] * 4

        # Rolling buffers (wall-clock time, current µA)
        self._pcb_rolling: list[list[tuple[float, float]]] = [[] for _ in range(4)]
        self._pcb_last_push = [0.0] * 4
        self._meter_samples: list[list[tuple[float, float]]] = [[] for _ in range(4)]
        self._meter_samples_lock = threading.Lock()

        # Cached list of visible VISA resources — refreshed only when the
        # meter count changes or the user hits ↻, so onchange doesn't
        # trigger N synchronous list_resources() scans.
        self._meter_devices_cache: list[str] = []

        # Log terminal
        self._log_text = None
        self._last_log_seq = -1
        self._calib_log_text = None
        self._last_calib_log_seq = -1
        self._css_injected = False
        # Boot-ID check runs once per session to force a browser reload if
        # the session was carried over from a previous Python process.
        self._boot_id_checked = False

    # -----------------------------------------------------------------------
    # Root UI
    # -----------------------------------------------------------------------
    def _build_ui(self):
        root = StyledContainer("root", 0, 0, _W, _H,
                               border=False, bg_color=True, color="#ffffff")
        root.style["left"] = "50%"
        root.style["transform"] = "translateX(-50%)"
        root.style["box-shadow"] = "none"

        for name in ("panel_ctrl", "panel_calib"):
            panel = StyledContainer(name, 0, _PANEL_Y, _W, _PANEL_H,
                                    border=False, bg_color=False, container=root)
            panel.style["display"] = "none"
            self._panels.append(panel)
        self._panels[0].style["display"] = "block"

        self._build_ctrl_panel(self._panels[0])
        self._build_calib_panel(self._panels[1])

        for i, label in enumerate(_TAB_LABELS):
            color = "#007BFF" if i == 0 else "#6c757d"
            btn = StyledButton(label, f"tab_btn_{i}",
                               _TAB_X, _TAB_Y0 + i * (_TAB_H + _TAB_GAP),
                               width=_TAB_W, height=_TAB_H,
                               normal_color=color, press_color="#0056B3",
                               font_size=90, container=root)
            btn.do_onclick(lambda idx=i: self._switch_tab(idx))
            self._tab_btns.append(btn)

        return root

    def _switch_tab(self, idx: int):
        for i, panel in enumerate(self._panels):
            panel.style["display"] = "block" if i == idx else "none"
        for i, btn in enumerate(self._tab_btns):
            btn.style["background-color"] = "#007BFF" if i == idx else "#6c757d"
            btn.normal_color = "#007BFF" if i == idx else "#6c757d"
        self._active_tab = idx

    # =======================================================================
    # Ctrl tab
    # =======================================================================
    def _build_ctrl_panel(self, container):
        # ---- ESP connection row (top) ----
        y = 12
        StyledLabel("ESP:", "lbl_esp", 20, y, width=42, height=28,
                    color="#444", flex=True,
                    justify_content="flex-start", container=container)

        self._port_dd = StyledDropDown("-- select port --", "port_dd",
                                       65, y, width=220, height=28,
                                       container=container)
        self._populate_ports()

        StyledButton("↺", "refresh_btn", 295, y, width=32, height=28,
                     normal_color="#6c757d", press_color="#495057",
                     container=container).do_onclick(self._on_refresh_ports)

        StyledLabel("Baud:", "lbl_baud", 350, y, width=42, height=28,
                    color="#444", flex=True, container=container)
        self._baud_dd = StyledDropDown("115200", "baud_dd",
                                       395, y, width=100, height=28,
                                       container=container)
        for b in ("9600", "115200", "230400", "460800", "921600"):
            if b != "115200":
                self._baud_dd.append(b)

        StyledLabel("Status:", "lbl_status_t", 525, y, width=52, height=28,
                    color="#444", flex=True, container=container)
        self._status_label = StyledLabel("●  Disconnected", "status_label",
                                         585, y, width=280, height=28,
                                         color=_DOT_OFF, flex=True,
                                         justify_content="flex-start",
                                         container=container)

        self._connect_btn = StyledButton("Connect", "connect_btn",
                                         875, y - 6, width=140, height=40,
                                         normal_color="#28a745",
                                         press_color="#1e7e34",
                                         container=container)
        self._connect_btn.do_onclick(self._on_connect_toggle)

        # Divider
        divider = StyledContainer("divider", 10, y + 55,
                                  _CONTENT_RIGHT_EDGE - 10, 1,
                                  border=True, line="1px solid #ccc",
                                  container=container)

        # ---- 4 channel columns ----
        y_base = y + 70
        col_xs = [5, 340, 675, 1010]
        for ch in range(4):
            self._build_channel_column(container, ch, col_xs[ch], y_base)

        # ---- Log terminal at bottom ----
        log_w = 800
        log_x = (_W - log_w) // 2
        log_y = y_base + 320
        self._log_text = gui.TextInput(singleline=False)
        self._log_text.css_position = "absolute"
        self._log_text.css_left = f"{log_x}px"
        self._log_text.css_top = f"{log_y}px"
        self._log_text.css_width = f"{log_w}px"
        self._log_text.css_height = "300px"
        self._log_text.style.update({
            "font-family": "monospace",
            "font-size": "12px",
            "background-color": "#1e1e1e",
            "color": "#f0f0f0",
            "border": "1px solid #444",
            "border-radius": "4px",
            "padding": "8px",
            "overflow-y": "auto",
            "white-space": "pre",
        })
        container.append(self._log_text, "log_text")

    def _build_channel_column(self, container, ch: int, x: int, y_base: int):
        # Row 1: Ch label + dot + mode indicator
        StyledLabel(f"Ch {ch}", f"lbl_ch_{ch}", x, y_base + 6, width=40, height=22,
                    color="#000", bold=True, flex=True, container=container)
        dot = StyledLabel("●", f"dot_{ch}", x + 42, y_base + 6, width=20, height=22,
                         color=_DOT_OFF, bold=True, flex=True, container=container)
        self._status_dots[ch] = dot
        mode_lbl = StyledLabel("OFF", f"mode_ind_{ch}", x + 64, y_base + 6,
                               width=80, height=22, color="#555",
                               flex=True, container=container)
        self._mode_indicator_lbls[ch] = mode_lbl

        # Row 2: ODR selector — fires send_odr on change (no Set button)
        StyledLabel("ODR:", f"odr_lbl_{ch}", x + 170, y_base + 6, width=40, height=22,
                    color="#444", flex=True, container=container)
        odr_dd = StyledDropDown(_ODR_OPTIONS[0], f"odr_dd_{ch}",
                                x + 210, y_base + 6, width=70, height=24,
                                container=container)
        for r in _ODR_OPTIONS[1:]:
            odr_dd.append(r)
        try:
            odr_dd.select_by_value(_ODR_DEFAULT)
        except Exception:
            pass
        odr_dd.onchange.do(lambda em, v, ch=ch: self._on_odr_change(ch, v))
        self._odr_dds[ch] = odr_dd
        StyledLabel("SPS", f"odr_unit_{ch}", x + 285, y_base + 6, width=30, height=22,
                    color="#444", flex=True, container=container)

        # Row 3: Mode buttons
        for mi, ml in enumerate(_MODE_LABELS):
            col, press = _MODE_OFF_ACTIVE if ml == "OFF" else _MODE_UNSEL
            btn = StyledButton(ml, f"mode_btn_{ch}_{mi}",
                               x + 20 + mi * 95, y_base + 45,
                               width=80, height=32,
                               normal_color=col, press_color=press,
                               container=container)
            btn.do_onclick(lambda ch=ch, m=ml: self._select_mode(ch, m))
            self._mode_btns[ch].append(btn)

        # Steady params container
        steady_c = StyledContainer(f"steady_c_{ch}", x, y_base + 88, 345, 90,
                                   border=False, bg_color=False, container=container)
        steady_c.style["display"] = "none"
        self._steady_containers[ch] = steady_c

        # Voltage row
        StyledLabel("Voltage:", f"lbl_v_{ch}", 10, 10, width=70, height=24,
                    color="#444", flex=True, container=steady_c)
        v_inp = StyledTextInput(f"v_inp_{ch}", 85, 10, width=70, height=24,
                                text="0.0", container=steady_c)
        v_inp.style["box-sizing"] = "border-box"
        self._voltage_inputs[ch] = v_inp
        StyledLabel("V", f"lbl_v_u_{ch}", 162, 10, width=15, height=24,
                    color="#444", flex=True, container=steady_c)
        StyledButton("−", f"v_minus_{ch}", 225, 7, width=40, height=30,
                     normal_color="#6c757d", press_color="#495057",
                     container=steady_c).do_onclick(
            lambda ch=ch: self._on_voltage_step(ch, -0.05))
        StyledButton("+", f"v_plus_{ch}", 270, 7, width=40, height=30,
                     normal_color="#6c757d", press_color="#495057",
                     container=steady_c).do_onclick(
            lambda ch=ch: self._on_voltage_step(ch, +0.05))

        # Timer row (steady only)
        timer_row = StyledContainer(f"timer_row_{ch}", 6, 48, 300, 30,
                                    border=False, bg_color=False, container=steady_c)
        timer_row.style.update({"display": "flex", "align-items": "center"})

        timer_cb = gui.CheckBox(checked=False)
        timer_cb.style.update({"margin": "0", "width": "16px", "height": "16px"})
        timer_row.append(timer_cb, f"timer_cb_{ch}")
        self._timer_checkboxes[ch] = timer_cb
        tlbl = gui.Label("Timer")
        tlbl.style.update({"color": "#444", "font-size": "15px", "margin-left": "6px"})
        timer_row.append(tlbl, f"timer_lbl_{ch}")

        dur_inp = StyledTextInput(f"dur_inp_{ch}", 0, 0, width=45, height=24,
                                  text="10")
        dur_inp.style.update({"position": "relative", "left": "0", "top": "0",
                              "margin-left": "16px", "display": "none"})
        timer_row.append(dur_inp, f"dur_inp_{ch}")
        self._duration_inputs[ch] = dur_inp

        tu = StyledDropDown("Min", f"tunit_{ch}", 0, 0, width=75, height=24)
        for u in ("Hour", "Day", "Month"):
            tu.append(u)
        tu.style.update({"position": "relative", "left": "0", "top": "0",
                         "margin-left": "8px", "display": "none"})
        timer_row.append(tu, f"tunit_{ch}")
        self._time_unit_dds[ch] = tu

        timer_cb.onchange.do(lambda em, v, ch=ch: self._on_timer_toggle(ch, v))

        # Sweep params container
        sweep_c = StyledContainer(f"sweep_c_{ch}", x, y_base + 88, 345, 90,
                                  border=False, bg_color=False, container=container)
        sweep_c.style["display"] = "none"
        self._sweep_containers[ch] = sweep_c

        StyledLabel("Range:", f"lbl_r_{ch}", 10, 13, width=70, height=24,
                    color="#444", flex=True, container=sweep_c)
        range_inp = StyledTextInput(f"range_{ch}", 85, 13, width=70, height=24,
                                    text="0.5", container=sweep_c)
        range_inp.style["box-sizing"] = "border-box"
        self._range_inputs[ch] = range_inp
        StyledLabel("V", f"lbl_r_u_{ch}", 162, 13, width=15, height=24,
                    color="#444", flex=True, container=sweep_c)

        StyledLabel("Step:", f"lbl_s_{ch}", 10, 48, width=70, height=24,
                    color="#444", flex=True, container=sweep_c)
        step_inp = StyledTextInput(f"step_{ch}", 85, 48, width=70, height=24,
                                   text="10", container=sweep_c)
        step_inp.style["box-sizing"] = "border-box"
        self._step_inputs[ch] = step_inp
        StyledLabel("mV", f"lbl_s_u_{ch}", 162, 48, width=25, height=24,
                    color="#444", flex=True, container=sweep_c)

        # Live readout (steady OR sweep)
        readout = StyledLabel("—", f"readout_{ch}", x + 5, y_base + 185,
                              width=300, height=24, color="#333",
                              container=container)
        readout.style.update({"display": "none", "font-family": "monospace",
                              "font-size": "13px", "font-weight": "bold"})
        self._readout_lbls[ch] = readout

        # Apply / Stop
        apply_btn = StyledButton("Apply", f"apply_{ch}", x, y_base + 220,
                                 width=150, height=40,
                                 normal_color="#28a745", press_color="#1e7e34",
                                 container=container)
        apply_btn.do_onclick(lambda ch=ch: self._on_apply(ch))
        self._apply_btns[ch] = apply_btn

        StyledButton("Stop", f"stop_{ch}", x + 160, y_base + 220,
                     width=150, height=40,
                     normal_color="#dc3545", press_color="#a71d2a",
                     container=container).do_onclick(
            lambda ch=ch: self._on_stop(ch))

        # Sweep plot link — appears between Apply/Stop and log when sweep is ready
        sweep_link = StyledLabel("📊 View sweep plot", f"sweep_link_{ch}",
                                 x + 5, y_base + 275, width=200, height=22,
                                 color="#007BFF", container=container)
        sweep_link.style.update({
            "display": "none",
            "cursor": "pointer",
            "text-decoration": "underline",
            "font-size": "13px",
        })
        sweep_link.onclick.do(lambda em, ch=ch: self._on_sweep_link_click(ch))
        self._sweep_link_lbls[ch] = sweep_link

        # "New sweep" indicator — blue dot right of the link. Turned on when a
        # fresh sweep finishes, cleared once the user opens the link.
        sweep_dot = StyledLabel("●", f"sweep_dot_{ch}",
                                x + 205, y_base + 275, width=18, height=22,
                                color="#007BFF", bold=True,
                                flex=True, container=container)
        sweep_dot.style.update({"display": "none", "font-size": "16px"})
        self._sweep_dot_lbls[ch] = sweep_dot

    # =======================================================================
    # Calib tab
    # =======================================================================
    def _build_calib_panel(self, container):
        # ---- Meter section (top) ----
        meter_y = 8
        # "Meters:" label + count dropdown share the SAME left column as the
        # meter rows below: label starts at x=20, dropdown at x=90 (same as
        # each row's port-dropdown left edge).
        StyledLabel("Meters:", "meters_lbl", 20, meter_y, width=65, height=24,
                    color="#222", bold=True, flex=True, container=container)
        self._meter_count_dd = StyledDropDown("0", "meter_count_dd",
                                              90, meter_y, width=55, height=24,
                                              container=container)
        self._meter_count_dd.style["box-sizing"] = "border-box"
        for n in ("1", "2", "3", "4"):
            self._meter_count_dd.append(n)
        self._meter_count_dd.onchange.do(
            lambda em, v: self._on_meter_count_change(v))

        # Container for dynamically-shown meter rows. Widened so each row
        # can carry its own "Poll: [_] s" input alongside the port/connect/ch
        # controls; the calib log sits to its right.
        self._meter_rows_container = StyledContainer("meter_rows_c",
                                                     20, meter_y + 30,
                                                     820, 130,
                                                     border=False,
                                                     bg_color=False,
                                                     container=container)
        # Prebuild 4 rows but hide them all
        for idx in range(4):
            self._build_meter_row(self._meter_rows_container, idx)

        # ---- Calib log (right of meter section) — no title, the dark
        # ---- monospace panel is self-explanatory. Shifted well right of
        # ---- the meter rows so the two blocks read as separate columns.
        cal_log_x, cal_log_y, cal_log_w, cal_log_h = 920, meter_y, 380, 155
        self._calib_log_text = gui.TextInput(singleline=False)
        self._calib_log_text.css_position = "absolute"
        self._calib_log_text.css_left = f"{cal_log_x}px"
        self._calib_log_text.css_top = f"{cal_log_y}px"
        self._calib_log_text.css_width = f"{cal_log_w}px"
        self._calib_log_text.css_height = f"{cal_log_h}px"
        self._calib_log_text.style.update({
            "font-family": "monospace",
            "font-size": "12px",
            "background-color": "#1e1e1e",
            "color": "#f0f0f0",
            "border": "1px solid #444",
            "border-radius": "4px",
            "padding": "6px",
            "overflow-y": "auto",
            "white-space": "pre-wrap",
            "box-sizing": "border-box",
            "resize": "none",
        })
        container.append(self._calib_log_text, "calib_log_text")

        # ---- Comparison plots (middle) ----
        # Extra top gap separates plots from the meter/log section above.
        plot_y0 = 200
        plot_col_x = [5, 720]
        plot_row_h = 225
        # Larger inter-row gap so the row-1 "Ch 2"/"Ch 3" labels don't
        # collide with the new live-readout labels sitting below row 0's
        # plot boxes.
        plot_row_gap = 78
        plot_row_y = [plot_y0, plot_y0 + plot_row_h + plot_row_gap]
        plot_w, plot_h = 700, plot_row_h

        for ch in range(4):
            col = ch % 2
            row = ch // 2
            self._build_calib_plot(container, ch,
                                   plot_col_x[col], plot_row_y[row],
                                   plot_w, plot_h)

        # ---- Factory calibration table (bottom, left-aligned) ----
        cal_y0 = plot_row_y[1] + plot_h + 55
        col_ch_x, col_r1k_x, col_rgain_x, col_vref_x, col_btn_x = 20, 110, 260, 410, 570
        inp_w = 130

        StyledLabel("Factory calibration", "calib_title",
                    col_ch_x, cal_y0, width=300, height=22,
                    color="#222", bold=True, flex=True,
                    justify_content="flex-start", container=container)

        def hdr(text, ident, xx, w):
            StyledLabel(text, ident, xx, cal_y0 + 32, width=w, height=22,
                        color="#555", bold=True, flex=True, container=container)
        hdr("Channel", "hdr_ch", col_ch_x, 70)
        hdr("R_1k (Ω)", "hdr_r1k", col_r1k_x, inp_w)
        hdr("R_gain (Ω)", "hdr_rgain", col_rgain_x, inp_w)
        hdr("DAC Vref (V)", "hdr_vref", col_vref_x, inp_w)

        for ch in range(4):
            y = cal_y0 + 68 + ch * 38
            StyledLabel(f"Ch {ch}", f"cal_lbl_{ch}",
                        col_ch_x, y, width=70, height=26,
                        color="#000", bold=True,
                        flex=True, container=container)
            for inp_arr, x_pos, ident_prefix, default in (
                (self._cal_r1k_inputs, col_r1k_x, "cal_r1k", _CAL_R1K_DEF),
                (self._cal_rgain_inputs, col_rgain_x, "cal_rgain", _CAL_RGAIN_DEF),
                (self._cal_vref_inputs, col_vref_x, "cal_vref", _CAL_VREF_DEF),
            ):
                inp = StyledTextInput(f"{ident_prefix}_{ch}", x_pos, y,
                                      width=inp_w, height=26, text=default,
                                      container=container)
                inp.style["box-sizing"] = "border-box"
                inp_arr[ch] = inp
            btn = StyledButton("Calibrate", f"recal_{ch}",
                               col_btn_x, y - 2, width=140, height=30,
                               normal_color="#28a745", press_color="#1e7e34",
                               container=container)
            btn.do_onclick(lambda ch=ch: self._on_recalibrate(ch))
            self._recal_btns[ch] = btn

            fetch = StyledButton("Fetch", f"fetch_{ch}",
                                 col_btn_x + 150, y - 2, width=100, height=30,
                                 normal_color="#007BFF", press_color="#0056B3",
                                 container=container)
            fetch.do_onclick(lambda ch=ch: self._on_fetch_calib(ch))
            self._fetch_calib_btns[ch] = fetch

    def _build_meter_row(self, parent, idx: int):
        y = idx * 30
        row_h = 24
        m_lbl = StyledLabel(f"Meter {idx}:", f"m_lbl_{idx}", 0, y, width=70,
                            height=row_h, color="#222", bold=True, flex=True,
                            container=parent)
        port_dd = StyledDropDown("-- select --", f"m_port_{idx}", 70, y,
                                 width=340, height=row_h, container=parent)
        self._meter_port_dds[idx] = port_dd

        refresh_btn = StyledButton("↺", f"m_refresh_{idx}", 415, y,
                                    width=28, height=row_h,
                                    normal_color="#6c757d", press_color="#495057",
                                    font_size=80, container=parent)
        refresh_btn.do_onclick(lambda idx=idx: self._on_meter_refresh(idx))

        cbtn = StyledButton("Connect", f"m_conn_{idx}", 448, y, width=90,
                            height=row_h,
                            normal_color="#28a745", press_color="#1e7e34",
                            font_size=80, container=parent)
        cbtn.do_onclick(lambda idx=idx: self._on_meter_connect_toggle(idx))
        self._meter_connect_btns[idx] = cbtn

        # Status is just a colored dot — the resource is already visible in port dd.
        stat = StyledLabel("●", f"m_stat_{idx}", 548, y,
                           width=20, height=row_h, color=_DOT_OFF,
                           bold=True, flex=True, container=parent)
        self._meter_status_lbls[idx] = stat

        ch_lbl = StyledLabel("Ch:", f"m_ch_lbl_{idx}", 600, y, width=25,
                             height=row_h, color="#444", flex=True,
                             container=parent)
        ch_dd = StyledDropDown("0", f"m_ch_{idx}", 628, y, width=55,
                               height=row_h, container=parent)
        for n in ("1", "2", "3"):
            ch_dd.append(n)
        try:
            ch_dd.select_by_value(str(idx))
        except Exception:
            pass
        ch_dd.onchange.do(lambda em, v, idx=idx: self._on_meter_channel_change(idx, v))
        self._meter_channel_dds[idx] = ch_dd

        # Per-meter polling interval. Live-editable — the meter i worker
        # re-reads self._meter_poll_s[i] on every loop iteration.
        poll_lbl = StyledLabel("Poll:", f"m_poll_lbl_{idx}", 700, y,
                                width=38, height=row_h, color="#444",
                                flex=True, justify_content="flex-start",
                                container=parent)
        poll_inp = StyledTextInput(f"m_poll_{idx}", 740, y,
                                   width=55, height=row_h,
                                   text=f"{_METER_POLL_S:g}", container=parent)
        poll_inp.style["box-sizing"] = "border-box"
        poll_inp.onchange.do(
            lambda em, v, idx=idx: self._on_poll_interval_change(idx, v))
        poll_unit = StyledLabel("s", f"m_poll_unit_{idx}", 800, y,
                                 width=15, height=row_h, color="#444",
                                 flex=True, justify_content="flex-start",
                                 container=parent)
        self._meter_poll_inputs[idx] = poll_inp

        # Force border-box so padding/border sit INSIDE the declared 24px
        # height — otherwise dropdowns render ~30px tall and lift out of
        # baseline with the labels/button/status dot next to them.
        for w in (port_dd, refresh_btn, cbtn, ch_dd, poll_inp):
            w.style["box-sizing"] = "border-box"
        # Vertically center label text against the 24px row height so
        # "Meter 0:" and "Ch:" share the same baseline as the dropdowns.
        for lbl in (m_lbl, stat, ch_lbl, poll_lbl, poll_unit):
            lbl.style["line-height"] = f"{row_h}px"

        # Hide the whole row by default; also parent container we'll style children directly.
        for widget_getter in (
            lambda: self._meter_port_dds[idx],
            lambda: self._meter_connect_btns[idx],
            lambda: self._meter_status_lbls[idx],
            lambda: self._meter_channel_dds[idx],
            lambda: self._meter_poll_inputs[idx],
        ):
            w = widget_getter()
            if w is not None:
                w.style["display"] = "none"
        # Hide the labels too — they were appended to parent by variable_name.
        for lbl_ident in (f"m_lbl_{idx}", f"m_ch_lbl_{idx}",
                           f"m_refresh_{idx}", f"m_poll_lbl_{idx}",
                           f"m_poll_unit_{idx}"):
            w = parent.children.get(lbl_ident)
            if w is not None:
                w.style["display"] = "none"

    def _build_calib_plot(self, container, ch: int, x: int, y: int, w: int, h: int):
        StyledLabel(f"Ch {ch}", f"cplot_lbl_{ch}", x + 5, y, width=100, height=22,
                    color="#000", bold=True, container=container)
        plot_c = StyledContainer(f"cplot_c_{ch}", x, y + 26, w, h,
                                 border=False, bg_color=False, container=container)
        # Aggressively kill anything the framework's default stylesheet may
        # add around the container/image — empty plots must render as blank
        # space, not a gray outlined box.
        for k, v in (("border", "0"), ("border-style", "none"),
                      ("outline", "none"), ("box-shadow", "none"),
                      ("background", "transparent")):
            plot_c.style[k] = v

        img = gui.Image("")
        img.css_position = "absolute"
        img.css_left = "0px"
        img.css_top = "0px"
        img.css_width = f"{w - 5}px"
        img.css_height = f"{h - 5}px"
        for k, v in (("border", "0"), ("border-style", "none"),
                      ("outline", "none"), ("background", "transparent"),
                      # Hide until a real image is set — an empty <img src="">
                      # renders as a placeholder box in Chromium.
                      ("display", "none")):
            img.style[k] = v
        plot_c.append(img, f"cplot_img_{ch}")
        self._calib_plot_imgs[ch] = img

        # Center the "no data" message vertically inside the plot area so
        # the empty state looks intentional instead of like a broken image.
        no_data = StyledLabel("No comparison data yet", f"cplot_nd_{ch}",
                              x + 5, y + 26 + (h // 2 - 11),
                              width=w - 10, height=22,
                              color="#aaa", flex=True,
                              container=container)
        self._calib_no_data_lbls[ch] = no_data

        # Live readout: PCB µA + each connected meter's µA for this channel.
        # Updated on every idle tick alongside the plot render.
        readout = StyledLabel("—", f"cplot_ro_{ch}",
                              x + 5, y + 26 + h + 6,
                              width=w - 10, height=22,
                              color="#333", flex=True,
                              justify_content="flex-start",
                              container=container)
        readout.style.update({
            "font-family": "monospace",
            "font-size": "13px",
            "font-weight": "bold",
        })
        self._calib_readout_lbls[ch] = readout

    # =======================================================================
    # Event handlers — Ctrl tab
    # =======================================================================
    def _on_refresh_ports(self):
        self._populate_ports()

    def _populate_ports(self):
        ports = VoltageSourceController.list_ports()
        self._port_dd.empty()
        if not ports:
            self._port_dd.append("No ports found")
            return
        for device, description in ports:
            self._port_dd.append(f"{device}  –  {description}")
        guess = VoltageSourceController.guess_port()
        if not guess:
            return
        for device, description in ports:
            if device == guess:
                try:
                    self._port_dd.select_by_value(f"{device}  –  {description}")
                    _log(f"[Port] Auto-selected {device}")
                except Exception:
                    pass
                break

    def _on_connect_toggle(self):
        if _vs.is_connected:
            self._graceful_disconnect()
            return
        val = self._port_dd.get_value() or ""
        if not val or val.startswith("No ports"):
            _log("[Connect] No port selected")
            return
        port = val.split(" – ", 1)[0].strip()
        try:
            baud = int((self._baud_dd.get_value() or "115200").strip())
        except ValueError:
            baud = 115200
        _log(f"[Connect] Opening {port} @ {baud}")
        _vs.connect(port, baud)

    def _graceful_disconnect(self):
        for ch in range(4):
            try:
                _vs.stop_steady_log(ch)
            except Exception:
                pass
            try:
                _vs.send_off(ch)
            except Exception:
                pass
        _vs.disconnect()

    def _select_mode(self, ch: int, mode: str):
        self._selected_modes[ch] = mode
        for mi, ml in enumerate(_MODE_LABELS):
            btn = self._mode_btns[ch][mi]
            if ml == mode:
                col, press = _MODE_OFF_ACTIVE if ml == "OFF" else _MODE_ACTIVE
            else:
                col, press = _MODE_UNSEL
            btn.style["background-color"] = col
            btn.normal_color = col
            btn.press_color = press
        self._steady_containers[ch].style["display"] = "block" if mode == "STEADY" else "none"
        self._sweep_containers[ch].style["display"] = "block" if mode == "SWEEP" else "none"

    def _on_voltage_step(self, ch: int, delta: float):
        try:
            v = float(self._voltage_inputs[ch].get_value() or "0.0")
        except ValueError:
            v = 0.0
        v = _clamp(v + delta, _V_MIN, _V_MAX)
        self._voltage_inputs[ch].set_value(f"{v:.3f}")

    def _on_timer_toggle(self, ch: int, value):
        show = value in (True, "true", "on", "1")
        display = "block" if show else "none"
        self._duration_inputs[ch].style["display"] = display
        self._time_unit_dds[ch].style["display"] = display

    def _timer_duration_seconds(self, ch: int):
        cb = self._timer_checkboxes[ch]
        if cb is None or not cb.get_value():
            return None
        try:
            raw = float(self._duration_inputs[ch].get_value() or "0")
            unit = self._time_unit_dds[ch].get_value() or "Min"
        except (ValueError, AttributeError):
            return None
        factor = {"Min": 60.0, "Hour": 3600.0, "Day": 86400.0, "Month": 2_592_000.0}
        return raw * factor.get(unit, 60.0)

    def _on_apply(self, ch: int):
        if not _vs.is_connected:
            _log(f"[Apply Ch {ch}] Not connected")
            return
        mode = self._selected_modes[ch]
        if mode == "OFF":
            _vs.stop_sweep_log(ch)
            _vs.stop_steady_log(ch)
            _vs.send_off(ch)
            _log(f"[Apply Ch {ch}] {ch},OFF")
            self._set_active_mode(ch, "OFF")
            return
        if mode == "STEADY":
            try:
                v_raw = float(self._voltage_inputs[ch].get_value() or "0.0")
            except ValueError:
                _log(f"[Apply Ch {ch}] Invalid voltage")
                return
            v = _clamp(v_raw, _V_MIN, _V_MAX)
            if v != v_raw:
                _log(f"[Apply Ch {ch}] Clamped V {v_raw} → {v}")
                self._voltage_inputs[ch].set_value(f"{v:.3f}")
            try:
                dur = float(self._duration_inputs[ch].get_value() or "10")
            except ValueError:
                dur = 10.0
            timer_on = bool(self._timer_checkboxes[ch].get_value())
            time_unit = self._time_unit_dds[ch].get_value() if timer_on else "Inf"
            _vs.stop_sweep_log(ch)
            _vs.stop_steady_log(ch)
            _vs.send_steady(ch, v, dur, time_unit)
            _log(f"[Apply Ch {ch}] {ch},STEADY,{v},{dur},{time_unit}")
            self._applied_voltages[ch] = v
            # Reset calib session for this channel so plot restarts fresh
            self._pcb_rolling[ch].clear()
            with self._meter_samples_lock:
                for idx in range(4):
                    if self._meter_channel[idx] == ch:
                        self._meter_samples[idx].clear()

            # Steady TXT log only when a timer is set AND at least one
            # connected meter is assigned to this channel.
            assigned_meters = [idx for idx in range(self._meter_count)
                               if self._meter_channel[idx] == ch
                               and _meter_connected(idx)]
            if timer_on and assigned_meters:
                dur_s = self._timer_duration_seconds(ch)
                path = _vs.start_steady_log(ch, v, dur_s, assigned_meters)
                if path:
                    _log(f"[Apply Ch {ch}] Steady TXT → {path}")
            self._set_active_mode(ch, "STEADY")
            return
        if mode == "SWEEP":
            try:
                r_raw = float(self._range_inputs[ch].get_value() or "1.0")
                step_mv = float(self._step_inputs[ch].get_value() or "10")
            except ValueError:
                _log(f"[Apply Ch {ch}] Invalid SWEEP params")
                return
            r = _clamp(r_raw, -_SWEEP_RANGE_MAX, _SWEEP_RANGE_MAX)
            if r != r_raw:
                _log(f"[Apply Ch {ch}] Clamped Range {r_raw} → {r}")
                self._range_inputs[ch].set_value(f"{r}")
            _vs.clear_channel_data(ch)
            self._sweep_active[ch] = False
            self._sweep_plot_data.pop(ch, None)
            self._sweep_link_lbls[ch].style["display"] = "none"
            if self._sweep_dot_lbls[ch] is not None:
                self._sweep_dot_lbls[ch].style["display"] = "none"
            path = _vs.start_sweep_log(ch)
            _vs.send_sweep(ch, r, step_mv)
            _log(f"[Apply Ch {ch}] {ch},SWEEP,{r},{step_mv}")
            if path:
                _log(f"[Apply Ch {ch}] Sweep TXT → {path}")
            self._set_active_mode(ch, "SWEEP")

    def _on_stop(self, ch: int):
        _vs.stop_steady_log(ch)
        _vs.send_off(ch)
        _log(f"[Stop Ch {ch}] sent {ch},OFF")
        self._set_active_mode(ch, "OFF")

    def _on_odr_change(self, ch: int, val):
        if not _vs.is_connected:
            _log(f"[ODR Ch {ch}] Not connected")
            return
        try:
            rate = float((val or _ODR_DEFAULT).strip())
        except (ValueError, AttributeError):
            _log(f"[ODR Ch {ch}] Invalid rate")
            return
        _vs.send_odr(ch, rate)
        _log(f"[ODR Ch {ch}] sent {ch},ODR,{rate}")

    def _set_active_mode(self, ch: int, mode: str):
        self._active_modes[ch] = mode
        dot = self._status_dots[ch]
        if dot is not None:
            dot.style["color"] = _DOT_OFF if mode == "OFF" else _DOT_ON
        readout = self._readout_lbls[ch]
        if readout is None:
            return
        if mode == "STEADY":
            readout.style["display"] = "block"
            readout.set_text(f"Set: {self._applied_voltages[ch]:.3f} V   |   Measured: —")
        elif mode == "SWEEP":
            readout.style["display"] = "block"
            readout.set_text("Voltage: —   |   Current: —")
        else:
            readout.style["display"] = "none"

    def _on_sweep_link_click(self, ch: int):
        pair = self._sweep_plot_data.get(ch)
        if not pair:
            return
        voltages, currents = pair
        # Clear the "new sweep" indicator now that the user has opened it.
        dot = self._sweep_dot_lbls[ch]
        if dot is not None:
            dot.style["display"] = "none"

        # Real interactive plot via Plotly: wheel zoom targets the data
        # (not a raster), box zoom by drag, no pan-on-drag, double-click
        # resets. Zoom-out is clamped to the data extents (+5% pad) so the
        # user can't drift into empty space. Requires internet access for
        # the CDN script tag.
        import json
        color = _CH_COLORS[ch]
        payload = {
            "voltages": voltages,
            "currents": currents,
            "color": color,
            "title": f"Ch {ch} sweep",
        }
        payload_json = json.dumps(payload)

        html = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>Ch {ch} Sweep</title>"
            "<script src='https://cdn.plot.ly/plotly-2.27.0.min.js'></script>"
            "<style>html,body{margin:0;height:100%;background:#fff;font-family:sans-serif;}"
            "#plot{width:100vw;height:100vh;}"
            "#hint{position:fixed;left:8px;bottom:8px;padding:4px 8px;"
            "background:rgba(0,0,0,0.55);color:#fff;font-size:12px;"
            "border-radius:4px;pointer-events:none;}"
            "</style></head><body>"
            "<div id='plot'></div>"
            "<div id='hint'>wheel = zoom into cursor &middot; drag = box zoom "
            "&middot; dbl-click = reset</div>"
            "<script>"
            f"var P={payload_json};"
            "var xMin=Math.min.apply(null,P.voltages);"
            "var xMax=Math.max.apply(null,P.voltages);"
            "var yMin=Math.min.apply(null,P.currents);"
            "var yMax=Math.max.apply(null,P.currents);"
            "var xPad=(xMax-xMin)*0.05||0.05;"
            "var yPad=(yMax-yMin)*0.05||0.05;"
            "var X0=xMin-xPad,X1=xMax+xPad;"
            "var Y0=yMin-yPad,Y1=yMax+yPad;"
            "function draw(){"
            "var el=document.getElementById('plot');"
            "Plotly.newPlot(el,[{"
            "x:P.voltages,y:P.currents,type:'scattergl',mode:'lines',"
            "line:{color:P.color,width:1.6},name:P.title"
            "}],{"
            "title:P.title,margin:{l:70,r:30,t:50,b:60},"
            "xaxis:{title:'Voltage (V)',showgrid:true,zeroline:false,"
            "gridcolor:'#ddd',range:[X0,X1],autorange:false},"
            "yaxis:{title:'Current (µA)',showgrid:true,zeroline:false,"
            "gridcolor:'#ddd',range:[Y0,Y1],autorange:false},"
            "dragmode:'zoom',hovermode:'closest'"
            "},{scrollZoom:true,displayModeBar:true,responsive:true,"
            "displaylogo:false});"
            # Clamp any zoom-out beyond the data extents back into bounds.
            "var clamping=false;"
            "el.on('plotly_relayout',function(ed){"
            "if(clamping)return;"
            "var xr=el.layout.xaxis.range,yr=el.layout.yaxis.range;"
            "var upd={},dirty=false;"
            "if(xr[0]<X0-1e-9){upd['xaxis.range[0]']=X0;dirty=true;}"
            "if(xr[1]>X1+1e-9){upd['xaxis.range[1]']=X1;dirty=true;}"
            "if(yr[0]<Y0-1e-9){upd['yaxis.range[0]']=Y0;dirty=true;}"
            "if(yr[1]>Y1+1e-9){upd['yaxis.range[1]']=Y1;dirty=true;}"
            "if(ed['xaxis.autorange']===true||ed['yaxis.autorange']===true){"
            "upd['xaxis.range']=[X0,X1];upd['yaxis.range']=[Y0,Y1];"
            "upd['xaxis.autorange']=false;upd['yaxis.autorange']=false;"
            "dirty=true;}"
            "if(dirty){clamping=true;"
            "Plotly.relayout(el,upd).then(function(){clamping=false;});}"
            "});"
            "}"
            "if(window.Plotly){draw();}else{"
            "var poll=setInterval(function(){if(window.Plotly){"
            "clearInterval(poll);draw();}},50);}"
            "window.addEventListener('resize',function(){"
            "if(window.Plotly)Plotly.Plots.resize(document.getElementById('plot'));"
            "});"
            "</script></body></html>"
        )
        html_b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
        # Open in an appropriately-sized popup, not a fullscreen tab.
        js = (
            "var w=window.open('','_blank',"
            "'width=1000,height=650,menubar=no,toolbar=no,location=no,status=no');"
            "if(w){"
            "w.document.open();"
            "w.document.write(atob('" + html_b64 + "'));"
            "w.document.close();"
            "}"
        )
        self.execute_javascript(js)

    # =======================================================================
    # Event handlers — Calib tab
    # =======================================================================
    def _on_recalibrate(self, ch: int):
        if not _vs.is_connected:
            _log(f"[Calibrate Ch {ch}] Not connected", calib=True)
            return

        # Empty field → NaN, which the firmware interprets as "echo my
        # stored calibration back to me". If the user leaves ALL three
        # blank we effectively ask the ESP for its current values.
        def _read(inp) -> float:
            s = (inp.get_value() or "").strip() if inp is not None else ""
            if not s:
                return float("nan")
            try:
                return float(s)
            except ValueError:
                return float("nan")

        r1k = _read(self._cal_r1k_inputs[ch])
        rg = _read(self._cal_rgain_inputs[ch])
        vref = _read(self._cal_vref_inputs[ch])

        _vs.send_calibration(ch, r1k, rg, vref)
        _log(f"[Calibrate Ch {ch}] R_1k={r1k} R_gain={rg} Vref={vref}",
             calib=True)

    def _on_fetch_calib(self, ch: int):
        """Ask the ESP to echo back its stored calibration for this channel
        (all-NaN send is the firmware's read-back signal). The echo lands
        in _on_calib_received and repopulates the input fields."""
        if not _vs.is_connected:
            _log(f"[Fetch Ch {ch}] Not connected", calib=True)
            return
        nan = float("nan")
        _vs.send_calibration(ch, nan, nan, nan)

    def _request_all_calibrations(self):
        """Fire off an all-NaN CALIBRATION for every channel so firmware
        echoes back the stored values (which populate the GUI fields via
        the calib callback). Spaced apart because the firmware
        calib_data_queue has depth 1 — sending too fast drops responses."""
        def _worker():
            # Small warm-up so the reader thread is up and the ESP finishes
            # whatever init it's doing right after we opened the port.
            time.sleep(0.5)
            nan = float("nan")
            for ch in range(4):
                if not _vs.is_connected:
                    return
                _vs.send_calibration(ch, nan, nan, nan)
                # Give the firmware time to drain its 1-slot echo queue
                # before we queue the next channel's response.
                time.sleep(0.15)
        threading.Thread(target=_worker, daemon=True,
                          name="req-calib").start()

    def _on_calib_received(self, channel_id: int, r_1k: float,
                            r_gain: float, dac_vref: float):
        """Firmware calib echo → push into the input fields for the channel
        the firmware reports. Fires for boot-time sends AND explicit
        Calibrate presses / all-NaN read-back, so every row is populated
        as soon as the ESP tells us its stored values."""
        if not 0 <= channel_id < 4:
            return
        if self._cal_r1k_inputs[channel_id] is not None:
            self._cal_r1k_inputs[channel_id].set_value(f"{r_1k:.4f}")
        if self._cal_rgain_inputs[channel_id] is not None:
            self._cal_rgain_inputs[channel_id].set_value(f"{r_gain:.4f}")
        if self._cal_vref_inputs[channel_id] is not None:
            self._cal_vref_inputs[channel_id].set_value(f"{dac_vref:.5f}")

    def _on_meter_count_change(self, val):
        try:
            n = int(val)
        except (TypeError, ValueError):
            n = 0
        n = _clamp(n, 0, 4)
        self._meter_count = n

        # One VISA scan, reused for every newly-visible row. Without this,
        # each row would trigger its own ~100–500 ms pyvisa.list_resources()
        # call on the WebSocket handler thread, freezing the dropdown until
        # they all return.
        need_scan = any(idx < n and self._meter_port_dds[idx] is not None
                        for idx in range(4))
        if need_scan:
            self._refresh_meter_devices_cache()

        for idx in range(4):
            visible = idx < n
            display = "block" if visible else "none"
            for w in (self._meter_port_dds[idx], self._meter_connect_btns[idx],
                      self._meter_status_lbls[idx], self._meter_channel_dds[idx],
                      self._meter_poll_inputs[idx]):
                if w is not None:
                    w.style["display"] = display
            for lbl_ident in (f"m_lbl_{idx}", f"m_ch_lbl_{idx}",
                               f"m_refresh_{idx}", f"m_poll_lbl_{idx}",
                               f"m_poll_unit_{idx}"):
                w = self._meter_rows_container.children.get(lbl_ident)
                if w is not None:
                    w.style["display"] = display
            if visible and self._meter_port_dds[idx] is not None:
                self._populate_meter_ports(idx, use_cache=True)
            if not visible and _meter_connected(idx):
                # Auto-disconnect meters that are now hidden
                self._disconnect_meter(idx)

    def _refresh_meter_devices_cache(self):
        try:
            self._meter_devices_cache = ScpiMeterController.list_devices()
        except Exception as e:
            _log(f"[Meter] VISA list error: {e}")
            self._meter_devices_cache = []

    def _populate_meter_ports(self, idx: int, use_cache: bool = False):
        if use_cache:
            resources = list(self._meter_devices_cache)
        else:
            try:
                resources = ScpiMeterController.list_devices()
            except Exception as e:
                _log(f"[Meter {idx}] VISA list error: {e}")
                resources = []
            self._meter_devices_cache = list(resources)
        dd = self._meter_port_dds[idx]
        dd.empty()
        if not resources:
            dd.append("No devices found")
            return
        for r in resources:
            dd.append(r)
        try:
            dd.select_by_value(resources[0])
        except Exception:
            pass

    def _on_meter_refresh(self, idx: int):
        # Explicit refresh — always re-scan and update the cache.
        self._populate_meter_ports(idx, use_cache=False)

    def _on_meter_connect_toggle(self, idx: int):
        if _meter_connected(idx):
            self._disconnect_meter(idx)
            return
        dd = self._meter_port_dds[idx]
        resource = (dd.get_value() or "").strip()
        if not resource or resource.startswith("No devices"):
            _log(f"[Meter {idx}] No device selected", calib=True)
            return
        m = ScpiMeterController()
        try:
            ok = m.connect(resource)
        except Exception as e:
            _log(f"[Meter {idx}] Connect error: {e}", calib=True)
            return
        if not ok:
            _log(f"[Meter {idx}] Connect failed on {resource}", calib=True)
            return
        _meters[idx] = m
        _log(f"[Meter {idx}] Connected {resource}", calib=True)
        # Start poller thread
        stop = threading.Event()
        self._meter_poll_stops[idx] = stop
        t = threading.Thread(target=self._meter_poll_worker,
                             args=(idx, stop), daemon=True,
                             name=f"meter-poll-{idx}")
        self._meter_poll_threads[idx] = t
        t.start()

    def _disconnect_meter(self, idx: int):
        stop = self._meter_poll_stops[idx]
        if stop is not None:
            stop.set()
        self._meter_poll_stops[idx] = None
        self._meter_poll_threads[idx] = None
        m = _meters[idx]
        if m is not None:
            try:
                m.disconnect()
            except Exception:
                pass
        _meters[idx] = None
        with self._meter_samples_lock:
            self._meter_samples[idx].clear()
        _log(f"[Meter {idx}] Disconnected", calib=True)

    def _on_meter_channel_change(self, idx: int, val):
        try:
            ch = int(val)
        except (TypeError, ValueError):
            return
        self._meter_channel[idx] = _clamp(ch, 0, 3)

    def _on_poll_interval_change(self, idx: int, val):
        try:
            v = float((val or "").strip())
        except (TypeError, ValueError):
            return
        if v < _METER_POLL_MIN_S:
            v = _METER_POLL_MIN_S
            inp = self._meter_poll_inputs[idx]
            if inp is not None:
                inp.set_value(f"{v:g}")
        self._meter_poll_s[idx] = v

    def _latest_pcb_current(self, ch: int) -> float | None:
        """Most recent PCB current sample for a channel (µA), or None."""
        buf = self._pcb_rolling[ch]
        if not buf:
            return None
        return buf[-1][1]

    def _update_calib_readout(self, ch: int):
        """Refresh the per-channel PCB+meter live readout under the plot."""
        lbl = self._calib_readout_lbls[ch]
        if lbl is None:
            return
        parts: list[str] = []
        pcb = self._latest_pcb_current(ch)
        if pcb is not None:
            parts.append(f"PCB: {pcb:.4f} µA")
        with self._meter_samples_lock:
            for idx in range(self._meter_count):
                if self._meter_channel[idx] != ch:
                    continue
                if not _meter_connected(idx):
                    continue
                buf = self._meter_samples[idx]
                if buf:
                    parts.append(f"Meter {idx}: {buf[-1][1]:.4f} µA")
        lbl.set_text("   |   ".join(parts) if parts else "—")

    # =======================================================================
    # Meter poller (one thread per connected meter)
    # =======================================================================
    def _meter_poll_worker(self, idx: int, stop: threading.Event):
        while not stop.is_set():
            m = _meters[idx]
            if m is None or not m.is_connected:
                break
            interval = max(_METER_POLL_MIN_S, self._meter_poll_s[idx])
            # Only sample when the assigned channel is actively in STEADY —
            # otherwise the plot fills up with meter-only data. Use a short
            # poll while idle so a long `interval` (e.g. 10 min) doesn't
            # delay the first STEADY sample by up to a full interval after
            # the user hits Apply.
            ch = self._meter_channel[idx]
            if self._active_modes[ch] != "STEADY":
                if stop.wait(min(interval, 0.2)):
                    return
                continue
            try:
                i_A = m.measure_current()
            except Exception as e:
                _log(f"[Meter {idx}] read error: {e}")
                if stop.wait(0.5):
                    return
                continue
            t = time.time()
            i_uA = i_A * 1e6
            with self._meter_samples_lock:
                buf = self._meter_samples[idx]
                buf.append((t, i_uA))
                cutoff = t - _ROLLING_WINDOW_S
                while buf and buf[0][0] < cutoff:
                    buf.pop(0)
            # Steady TXT log: one row per meter poll, with a dedicated
            # column for THIS meter and the latest PCB reading for reference.
            _vs.write_steady_row(ch, t, self._applied_voltages[ch],
                                 self._latest_pcb_current(ch), idx, i_uA)
            if stop.wait(interval):
                return

    # =======================================================================
    # Idle loop — runs on each remi tick
    # =======================================================================
    def idle(self):
        # One-shot: kill focus outline on buttons/inputs
        if not self._css_injected:
            try:
                self.execute_javascript(
                    "if(!document.getElementById('_no_outline')){"
                    "var s=document.createElement('style');"
                    "s.id='_no_outline';"
                    "s.textContent='button:focus,button:focus-visible,input:focus,select:focus,textarea:focus{outline:none !important;}';"
                    "document.head.appendChild(s);}")
                self._css_injected = True
            except Exception:
                pass

        # ESP connection state
        connected = _vs.is_connected
        if connected != self._was_connected:
            if connected:
                self._status_label.set_text(f"●  {_vs.connected_port}")
                self._status_label.style["color"] = _DOT_ON
                self._connect_btn.set_text("Disconnect")
                self._connect_btn.style["background-color"] = "#dc3545"
                self._connect_btn.normal_color = "#dc3545"
                self._connect_btn.press_color = "#a71d2a"
                threading.Thread(target=_vs.start_reading,
                                 kwargs={"output_dir": "output"},
                                 daemon=True).start()
                _log(f"[Connected] {_vs.connected_port}")
            else:
                self._status_label.set_text("●  Disconnected")
                self._status_label.style["color"] = _DOT_OFF
                self._connect_btn.set_text("Connect")
                self._connect_btn.style["background-color"] = "#28a745"
                self._connect_btn.normal_color = "#28a745"
                self._connect_btn.press_color = "#1e7e34"
                _vs.stop_reading()
                _log("[Disconnected]")
                for ch in range(4):
                    self._select_mode(ch, "OFF")
                    self._sweep_active[ch] = False
                    self._active_modes[ch] = "OFF"
                    if self._status_dots[ch] is not None:
                        self._status_dots[ch].style["color"] = _DOT_OFF
                    if self._readout_lbls[ch] is not None:
                        self._readout_lbls[ch].style["display"] = "none"
            self._was_connected = connected

        # Meter connection state (per idx)
        for idx in range(4):
            state = _meter_connected(idx)
            if state == self._meter_was_connected[idx]:
                continue
            btn = self._meter_connect_btns[idx]
            stat = self._meter_status_lbls[idx]
            if btn is not None and stat is not None:
                if state:
                    stat.style["color"] = _DOT_ON
                    btn.set_text("Disconnect")
                    btn.style["background-color"] = "#dc3545"
                    btn.normal_color = "#dc3545"
                    btn.press_color = "#a71d2a"
                else:
                    stat.style["color"] = _DOT_OFF
                    btn.set_text("Connect")
                    btn.style["background-color"] = "#28a745"
                    btn.normal_color = "#28a745"
                    btn.press_color = "#1e7e34"
            self._meter_was_connected[idx] = state

        # Per-channel data
        now = time.time()
        for ch in range(4):
            if _vs.has_new_data(ch):
                data = _vs.get_channel_data(ch)
                if data:
                    latest = data[-1]
                    # Firmware mode: 0=SWEEP, 1=STEADY, else OFF
                    if latest.mode >= 2:
                        fw = "OFF"
                    elif latest.mode == 0:
                        fw = "SWEEP"
                    else:
                        fw = "STEADY"

                    dot = self._status_dots[ch]
                    if dot is not None:
                        dot.style["color"] = _DOT_OFF if fw == "OFF" else _DOT_ON
                    self._active_modes[ch] = fw
                    if self._mode_indicator_lbls[ch] is not None:
                        self._mode_indicator_lbls[ch].set_text(fw)

                    readout = self._readout_lbls[ch]
                    if fw == "STEADY":
                        if readout is not None:
                            readout.style["display"] = "block"
                            readout.set_text(
                                f"Set: {self._applied_voltages[ch]:.3f} V   |   "
                                f"Measured: {latest.current:.5f} µA")
                        # Push to PCB rolling buffer (throttled)
                        if now - self._pcb_last_push[ch] >= _PCB_LOG_INTERVAL:
                            buf = self._pcb_rolling[ch]
                            buf.append((now, latest.current))
                            cutoff = now - _ROLLING_WINDOW_S
                            while buf and buf[0][0] < cutoff:
                                buf.pop(0)
                            self._pcb_last_push[ch] = now
                    elif fw == "SWEEP":
                        self._sweep_active[ch] = True
                        if readout is not None:
                            readout.style["display"] = "block"
                            readout.set_text(
                                f"Voltage: {latest.voltage:.3f} V   |   "
                                f"Current: {latest.current:.5f} µA")
                    else:  # OFF
                        if self._sweep_active[ch]:
                            # Sweep just ended — render final plot and expose the link
                            self._render_sweep_plot(ch, data)
                            self._sweep_active[ch] = False
                        if readout is not None:
                            readout.style["display"] = "none"

            # Throttled calib plot render
            if now - self._last_calib_render[ch] >= 1.0 / _CALIB_RENDER_HZ:
                self._render_calib_plot(ch)
                self._last_calib_render[ch] = now
            # Live readout under the plot — cheap, refresh every tick.
            self._update_calib_readout(ch)

        # Log terminal
        if _log_seq != self._last_log_seq and self._log_text is not None:
            self._log_text.set_text("\n".join(_log_lines))
            self._last_log_seq = _log_seq
            eid = self._log_text.identifier
            self.execute_javascript(
                f"setTimeout(function(){{var e=document.getElementById('{eid}');"
                f"if(e)e.scrollTop=e.scrollHeight;}},30);")

        # Calib-filtered log (meter connect + recalibration only)
        if (_calib_log_seq != self._last_calib_log_seq
                and self._calib_log_text is not None):
            self._calib_log_text.set_text("\n".join(_calib_log_lines))
            self._last_calib_log_seq = _calib_log_seq
            eid = self._calib_log_text.identifier
            self.execute_javascript(
                f"setTimeout(function(){{var e=document.getElementById('{eid}');"
                f"if(e)e.scrollTop=e.scrollHeight;}},30);")

    # =======================================================================
    # Plot rendering
    # =======================================================================
    def _render_sweep_plot(self, ch: int, data):
        """Cache raw sweep data so the Plotly popup can zoom into the plot
        (not scale a raster). Also toggles the 'new sweep' blue dot on."""
        if not data:
            return
        voltages = [d.voltage for d in data]
        currents = [d.current for d in data]
        self._sweep_plot_data[ch] = (voltages, currents)
        link = self._sweep_link_lbls[ch]
        if link is not None:
            link.style["display"] = "block"
        dot = self._sweep_dot_lbls[ch]
        if dot is not None:
            dot.style["display"] = "inline-block"

    def _render_calib_plot(self, ch: int):
        pcb = list(self._pcb_rolling[ch])
        meter_datasets = []
        with self._meter_samples_lock:
            for idx in range(self._meter_count):
                if self._meter_channel[idx] == ch and _meter_connected(idx):
                    meter_datasets.append((idx, list(self._meter_samples[idx])))

        if not pcb and not meter_datasets:
            if self._calib_no_data_lbls[ch] is not None:
                self._calib_no_data_lbls[ch].style["display"] = "block"
            if self._calib_plot_imgs[ch] is not None:
                self._calib_plot_imgs[ch].set_image("")
                self._calib_plot_imgs[ch].style["display"] = "none"
            return

        try:
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_agg import FigureCanvasAgg
            from matplotlib.ticker import AutoMinorLocator, MultipleLocator
        except Exception as e:
            _log(f"[Calib plot Ch {ch}] matplotlib import failed: {e}")
            return

        # Shared t0 = earliest sample across all series
        all_times = [t for t, _ in pcb]
        all_ys: list[float] = []
        for _, ds in meter_datasets:
            all_times.extend(t for t, _ in ds)
            all_ys.extend(y for _, y in ds)
        all_ys.extend(y for _, y in pcb)
        if not all_times:
            return
        t0 = min(all_times)

        fig = Figure(figsize=(7.0, 2.8), dpi=100)
        canvas = FigureCanvasAgg(fig)
        ax = fig.add_subplot(111)
        if pcb:
            xs = [t - t0 for t, _ in pcb]
            ys = [i for _, i in pcb]
            ax.plot(xs, ys, color=_CH_COLORS[ch], linewidth=1.2, label="PCB")
        for idx, ds in meter_datasets:
            if not ds:
                continue
            xs = [t - t0 for t, _ in ds]
            ys = [i for _, i in ds]
            ax.plot(xs, ys, color=_METER_COLORS[idx % 4], linewidth=1.0,
                    label=f"Meter {idx}")
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Current (µA)")

        # Adaptive y-limits + grid density. A fixed 0.1 µA locator was
        # producing hundreds of ticks whenever data spanned more than a few
        # µA — matplotlib then dropped tick labels and the grid entirely.
        # Instead we pick the finest step whose tick count stays readable.
        if all_ys:
            ymin, ymax = min(all_ys), max(all_ys)
            span = max(ymax - ymin, 0.5)  # enforce a visible baseline span
            pad = span * 0.15
            y_lo, y_hi = ymin - pad, ymax + pad
            ax.set_ylim(y_lo, y_hi)
            view = y_hi - y_lo
            if view < 1.5:
                major = 0.1
            elif view < 6:
                major = 0.5
            elif view < 30:
                major = 1.0
            elif view < 150:
                major = 5.0
            else:
                major = None
            if major is not None:
                ax.yaxis.set_major_locator(MultipleLocator(major))
                ax.yaxis.set_minor_locator(MultipleLocator(major / 5))
        ax.xaxis.set_minor_locator(AutoMinorLocator())
        ax.grid(True, which="major", linestyle=":", alpha=0.6)
        ax.grid(True, which="minor", axis="y", linestyle=":", alpha=0.25)
        ax.legend(fontsize=8, loc="best")
        fig.tight_layout()

        buf = io.BytesIO()
        canvas.print_png(buf)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        if self._calib_plot_imgs[ch] is not None:
            self._calib_plot_imgs[ch].set_image(f"data:image/png;base64,{b64}")
            self._calib_plot_imgs[ch].style["display"] = "block"
        if self._calib_no_data_lbls[ch] is not None:
            self._calib_no_data_lbls[ch].style["display"] = "none"


# ===========================================================================
# Entry point
# ===========================================================================
def _print_server_urls(port: int):
    import socket
    urls = [f"http://127.0.0.1:{port}/", f"http://localhost:{port}/"]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan = s.getsockname()[0]
        s.close()
        if lan and lan != "127.0.0.1":
            urls.append(f"http://{lan}:{port}/")
    except Exception:
        pass
    print("=" * 60, flush=True)
    print("Voltage Source GUI available at:", flush=True)
    for u in urls:
        print(f"  → {u}", flush=True)
    print("=" * 60, flush=True)


def run_remi():
    cfg = get_window_config("voltage_source")
    port = cfg.get("port", 8006)
    _print_server_urls(port)
    start(VoltageSourceApp, address="0.0.0.0", port=port,
          start_browser=False, multiple_instance=True, enable_file_cache=False)


def _shutdown_channels():
    if not _vs.is_connected:
        return
    print("[VoltageSource] Shutdown: turning off all channels", flush=True)
    for ch in range(4):
        try:
            _vs.send_off(ch)
        except Exception:
            pass
    _vs.disconnect()


def _install_shutdown_hooks():
    atexit.register(_shutdown_channels)

    def _on_signal(_signum, _frame):
        sys.exit(0)

    for sig_name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass


def run():
    try:
        _install_shutdown_hooks()
        cfg = get_window_config("voltage_source")
        port = cfg.get("port", 8006)
        if platform.system() == "Windows":
            threading.Thread(target=run_remi, daemon=True).start()
            webview.create_window(
                cfg.get("title", "Voltage Source"),
                f"http://127.0.0.1:{port}",
                width=cfg.get("width", 1400),
                height=cfg.get("height", 810),
                resizable=True,
                hidden=True,
            )
            webview.start()
        else:
            run_remi()
    except Exception:
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    run()