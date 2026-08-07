import sys
import os
import threading
import platform
import time
import webview
import io
import base64
import signal
import atexit
import csv
import datetime

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from remi import App, start
import remi.gui as gui
from layout.lib_gui import (
    StyledContainer,
    StyledButton,
    StyledLabel,
    StyledDropDown,
    StyledTextInput,
    StyledCheckBox,
)
from layout.window_config import get_window_config
from controller.vs_controller import VoltageSourceController
from controller.scpi_meter_controller import ScpiMeterController

import logging
logging.getLogger("remi.server").setLevel(logging.ERROR)

_BAUD_RATES = ["115200", "9600", "57600", "230400"]
_TAB_LABELS = ["Ctrl", "Plots", "Time", "Calib"]
_TAB_W = 75
_TAB_H = 50
_TAB_GAP = 6
_TAB_FONT = 90
_PANEL_Y = 20
_PANEL_H = 1040
_W = 1430
_H = 1060
_TAB_X = _W - _TAB_W - 10           # 1345
_TAB_Y0 = 60
_CONTENT_RIGHT_EDGE = _TAB_X - 15   # 1330 — widgets should not exceed this

_CH_COLORS = {0: "#007BFF", 1: "#28a745", 2: "#dc3545", 3: "#fd7e14"}

_V_MIN = -5.0
_V_MAX = 5.0
_SWEEP_RANGE_MAX = 1.5

_DOT_OFF_COLOR = "#dc3545"
_DOT_ON_COLOR = "#28a745"

_MODE_LABELS = ["OFF", "STEADY", "SWEEP"]
_MODE_COLORS = {
    "OFF":    ("#495057", "#343a40"),
    "STEADY": ("#28a745", "#1e7e34"),
    "SWEEP":  ("#28a745", "#1e7e34"),
}
_MODE_UNSEL_COLOR = "#adb5bd"
_MODE_UNSEL_PRESS = "#868e96"

_ODR_OPTIONS = ["1.25", "2.5", "5", "10", "16", "20", "49", "59", "100", "200"]
_ODR_DEFAULT = "10"

_CAL_R1K_DEFAULT = "1000.0"
_CAL_RGAIN_DEFAULT = "401.0"
_CAL_VREF_DEFAULT = "2.5"


def _clamp_voltage(v: float) -> float:
    return max(_V_MIN, min(_V_MAX, v))


def _clamp_sweep_range(v: float) -> float:
    return max(-_SWEEP_RANGE_MAX, min(_SWEEP_RANGE_MAX, v))

# --------------------------------------------------------------------------
# Module-level log buffer
# --------------------------------------------------------------------------

_log_lines: list = []
_log_seq: int = 0
_LOG_BUFFER_MAX = 2000


def _vs_log(msg: str):
    global _log_seq
    _log_lines.append(msg)
    if len(_log_lines) > _LOG_BUFFER_MAX:
        del _log_lines[:len(_log_lines) - _LOG_BUFFER_MAX]
    _log_seq += 1
    print(msg, flush=True)


_vs = VoltageSourceController()
_vs.log_callback = _vs_log

# --------------------------------------------------------------------------
# Meter (SCPI DMM over VISA/USBTMC).
# Held at module level so a single instance is shared across the GUI.
# ScpiMeterController is internally thread-safe (its own _io_lock).
# --------------------------------------------------------------------------
_meter: ScpiMeterController | None = None

_METER_OUTPUT_DIR = "output"


def _meter_is_connected() -> bool:
    return _meter is not None and _meter.is_connected


class VoltageSourceApp(App):

    def __init__(self, *args, **kwargs):
        # Connection widgets
        self._connect_btn = None
        self._port_dd = None
        self._baud_dd = None
        self._status_label = None
        self._log_text = None
        self._was_connected = False

        # meter connection widgets + state
        self._meter_port_dd = None
        self._meter_status_label = None
        self._meter_connect_btn = None
        self._meter_was_connected = False
        self._meter_connecting = False  

        # Per-channel meter compare widgets/state
        self._meter_compare_cbs: list = [None] * 4
        self._meter_poll_inputs: list = [None] * 4     # ms
        self._meter_compare_data: dict = {i: [] for i in range(4)}   # (elapsed_s, current_A, voltage_V)
        self._meter_compare_threads: list = [None] * 4
        self._meter_compare_stops: list = [None] * 4
        self._meter_compare_paths: list = [""] * 4

        # Tab system
        self._tab_btns: list = []
        self._panels: list = []
        self._active_tab: int = 0  # 0=connection, 1=channels
        self._last_log_seq = 0

        # Per-channel widgets (indexed 0-3)
        self._mode_btns: list = [None] * 4        # [ [off_btn, steady_btn, sweep_btn], ... ]
        self._selected_modes: list = ["OFF"] * 4  # user-selected mode per channel
        self._steady_containers: list = [None] * 4
        self._sweep_containers: list = [None] * 4
        self._voltage_inputs: list = [None] * 4
        self._duration_inputs: list = [None] * 4
        self._time_unit_dds: list = [None] * 4
        self._timer_checkboxes: list = [None] * 4
        self._range_inputs: list = [None] * 4
        self._step_inputs: list = [None] * 4
        self._status_dots: list = [None] * 4
        self._steady_readouts: list = [None] * 4
        self._applied_voltages: list = [0.0] * 4
        self._active_modes: list = ["OFF"] * 4
        self._sweep_active: list = [False] * 4
        self._apply_btns: list = [None] * 4  # per-channel, so we can rename Apply→Start for SWEEP
        self._css_injected: bool = False     # inject focus-outline CSS on first idle tick

        # Per-channel plot widgets (Channels tab)
        self._mode_indicator_labels: list = [None] * 4
        self._plot_containers: list = [None] * 4
        self._no_plot_labels: list = [None] * 4
        self._plot_imgs: list = [None] * 4

        # Per-channel time-vs-current plot widgets (Time tab)
        self._time_plot_containers: list = [None] * 4
        self._no_time_plot_labels: list = [None] * 4
        self._time_plot_imgs: list = [None] * 4
        self._last_time_render: list = [0.0] * 4

        # Per-channel calibration plot widgets (Calibration tab)
        self._calib_plot_containers: list = [None] * 4
        self._no_calib_plot_labels: list = [None] * 4
        self._calib_plot_imgs: list = [None] * 4

        # Per-channel calibration input widgets (Calibration tab)
        self._cal_r1k_inputs: list = [None] * 4
        self._cal_rgain_inputs: list = [None] * 4
        self._cal_vref_inputs: list = [None] * 4
        self._recalibrate_btns: list = [None] * 4

        # Per-channel ODR selector widgets (Ctrl tab)
        self._odr_dds: list = [None] * 4
        self._odr_set_btns: list = [None] * 4

        if "editing_mode" not in kwargs:
            super().__init__(*args, **{"static_file_path": {"my_res": "./res/"}})

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def main(self):
        try:
            return self._build_ui()
        except Exception:
            import traceback
            traceback.print_exc()
            return gui.Label("Error — check terminal")

    def _build_ui(self):
        root = StyledContainer(
            "root", 0, 0, _W, _H,
            border=False, bg_color=True, color="#ffffff",
            position="absolute",
        )
        # Horizontally center the whole GUI in the viewport; kill any
        # ambient shadow the browser / remi might apply to the outer rim.
        root.style["left"] = "50%"
        root.style["transform"] = "translateX(-50%)"
        root.style["box-shadow"] = "none"

        # --- Connection panel (Tab 0) ---
        conn_panel = StyledContainer(
            "panel_conn", 0, _PANEL_Y, _W, _PANEL_H,
            border=False, bg_color=False,
            position="absolute",
            container=root,
        )
        conn_panel.style["display"] = "block"
        self._panels.append(conn_panel)
        self._build_connection_panel(conn_panel)

        # --- Channels panel (Tab 1) ---
        ch_panel = StyledContainer(
            "panel_channels", 0, _PANEL_Y, _W, _PANEL_H,
            border=False, bg_color=False,
            position="absolute",
            container=root,
        )
        ch_panel.style["display"] = "none"
        self._panels.append(ch_panel)
        self._build_channels_panel(ch_panel)

        # --- Time-vs-current panel (Tab 2) ---
        time_panel = StyledContainer(
            "panel_time", 0, _PANEL_Y, _W, _PANEL_H,
            border=False, bg_color=False,
            position="absolute",
            container=root,
        )
        time_panel.style["display"] = "none"
        self._panels.append(time_panel)
        self._build_time_panel(time_panel)

        # --- Calibration panel (Tab 3) ---
        calib_panel = StyledContainer(
            "panel_calib", 0, _PANEL_Y, _W, _PANEL_H,
            border=False, bg_color=False,
            position="absolute",
            container=root,
        )
        calib_panel.style["display"] = "none"
        self._panels.append(calib_panel)
        self._build_calib_panel(calib_panel)

        # --- Tab bar (right-edge sidebar, stacked vertically) — appended
        # LAST so they layer on top of the panels and remain clickable.
        for i, label in enumerate(_TAB_LABELS):
            color = "#007BFF" if i == 0 else "#6c757d"
            btn = StyledButton(
                label, f"tab_btn_{i}",
                _TAB_X, _TAB_Y0 + i * (_TAB_H + _TAB_GAP),
                width=_TAB_W, height=_TAB_H,
                normal_color=color, press_color="#0056B3",
                font_size=_TAB_FONT,
                container=root,
            )
            btn.do_onclick(lambda idx=i: self._switch_tab(idx))
            self._tab_btns.append(btn)

        print("main() built successfully", flush=True)
        return root

    # ------------------------------------------------------------------
    # Tab 1 — Connection panel
    # ------------------------------------------------------------------

    def _build_connection_panel(self, container):
        # ---- Connection row — shifted down for breathing room under the tabs.
        _CONN_Y = 40

        # Port row is horizontally centered in the usable area 0..1200
        # (leaving 1200+ for the top-right tabs). Content width ≈ 930.
        StyledLabel("Port:", "lbl_port", 135, _CONN_Y, width=42, height=28,
                    color="#555", flex=True, justify_content="flex-start",
                    container=container)
        self._port_dd = StyledDropDown(
            "-- select port --", "port_dd", 180, _CONN_Y, width=140, height=28,
            container=container,
        )
        self._populate_ports()

        refresh_btn = StyledButton(
            "↺", "refresh_btn", 330, _CONN_Y, width=32, height=28,
            normal_color="#6c757d", press_color="#495057",
            container=container,
        )
        refresh_btn.do_onclick(self._on_refresh)

        StyledLabel("Baud:", "lbl_baud", 415, _CONN_Y, width=42, height=28,
                    color="#555", flex=True, justify_content="flex-start",
                    container=container)
        self._baud_dd = StyledDropDown(
            "115200", "baud_dd", 460, _CONN_Y, width=90, height=28,
            container=container,
        )
        for rate in _BAUD_RATES[1:]:
            self._baud_dd.append(rate)

        StyledLabel("Status:", "lbl_status_title", 590, _CONN_Y, width=52, height=28,
                    color="#555", flex=True, justify_content="flex-start",
                    container=container)
        self._status_label = StyledLabel(
            "●  Disconnected", "status_lbl",
            650, _CONN_Y, width=250, height=28,
            color="#dc3545", bold=True,
            flex=True, justify_content="flex-start",
            container=container,
        )

        self._connect_btn = StyledButton(
            "Connect", "connect_btn",
            925, _CONN_Y - 6, width=140, height=40,
            normal_color="#28a745", press_color="#1e7e34",
            font_size=105,
            container=container,
        )
        self._connect_btn.do_onclick(self._on_connect_toggle)

        # ---- meter row — same X layout as ESP row, one line below.
        _METER_Y = _CONN_Y + 45
        StyledLabel("meter:", "lbl_meter_port", 135, _METER_Y, width=42, height=28,
                    color="#555", flex=True, justify_content="flex-start",
                    container=container)
        self._meter_port_dd = StyledDropDown(
            "-- select port --", "meter_port_dd", 180, _METER_Y, width=140, height=28,
            container=container,
        )
        self._populate_meter_ports()

        meter_refresh_btn = StyledButton(
            "↺", "meter_refresh_btn", 330, _METER_Y, width=32, height=28,
            normal_color="#6c757d", press_color="#495057",
            container=container,
        )
        meter_refresh_btn.do_onclick(self._on_meter_refresh_ports)

        StyledLabel("Status:", "lbl_meter_status_title", 590, _METER_Y, width=52, height=28,
                    color="#555", flex=True, justify_content="flex-start",
                    container=container)
        self._meter_status_label = StyledLabel(
            "●  Disconnected", "meter_status_lbl",
            650, _METER_Y, width=250, height=28,
            color="#dc3545", bold=True,
            flex=True, justify_content="flex-start",
            container=container,
        )

        self._meter_connect_btn = StyledButton(
            "Connect", "meter_connect_btn",
            925, _METER_Y - 6, width=140, height=40,
            normal_color="#28a745", press_color="#1e7e34",
            font_size=105,
            container=container,
        )
        self._meter_connect_btn.do_onclick(self._on_meter_connect_toggle)

        # Divider — below both ESP and meter rows. Stops before the sidebar.
        StyledContainer(
            "divider", 10, _METER_Y + 55, _CONTENT_RIGHT_EDGE - 10, 1,
            border=True, bg_color=False,
            position="absolute",
            container=container,
            line="1px solid #ccc",
        )

        # ---- 4 channel columns — centered in the area left of the right
        # sidebar. 4 * 310 col + 3 * 20 gap = 1300 → fits within
        # _CONTENT_RIGHT_EDGE (1330) with ~15 px margin on each side.
        _Y_BASE = _METER_Y + 65
        _CH_X = [22, 352, 682, 1012]
        _COL_W = 310

        for ch in range(4):
            x = _CH_X[ch]

            # "Ch N" bold label + status dot
            StyledLabel(
                f"Ch {ch}", f"conn_lbl_ch_{ch}",
                x, _Y_BASE + 6, width=40, height=22,
                color="#222", bold=True,
                flex=True, justify_content="flex-start",
                container=container,
            )
            dot = StyledLabel(
                "●", f"ch_dot_{ch}",
                x + 40, _Y_BASE + 6, width=20, height=22,
                color=_DOT_OFF_COLOR, bold=True,
                flex=True, justify_content="flex-start",
                container=container,
            )
            self._status_dots[ch] = dot

            # ODR selector — shares the top row with the Ch label + dot so it
            # doesn't fight for vertical space with the mode buttons below.
            # Sends "{ch},OFF,ODR,{rate}" only when Set is clicked (matches
            # the Apply pattern used for STEADY/SWEEP).
            StyledLabel(
                "ODR:", f"odr_lbl_{ch}",
                x + 68, _Y_BASE + 6, width=35, height=22,
                color="#444",
                flex=True, justify_content="flex-start",
                container=container,
            )
            # First item passed to StyledDropDown becomes index 0, so seed
            # with the lowest rate and append the rest in order. Then
            # select the firmware default (10 SPS) as the initial value.
            odr_dd = StyledDropDown(
                _ODR_OPTIONS[0], f"odr_dd_{ch}",
                x + 108, _Y_BASE + 6, width=70, height=24,
                container=container,
            )
            for rate in _ODR_OPTIONS[1:]:
                odr_dd.append(rate)
            try:
                odr_dd.select_by_value(_ODR_DEFAULT)
            except Exception:
                pass
            self._odr_dds[ch] = odr_dd

            StyledLabel(
                "SPS", f"odr_unit_{ch}",
                x + 183, _Y_BASE + 6, width=30, height=22,
                color="#444",
                flex=True, justify_content="flex-start",
                container=container,
            )
            odr_set_btn = StyledButton(
                "Set", f"odr_set_btn_{ch}",
                x + 220, _Y_BASE + 4, width=70, height=26,
                normal_color="#6c757d", press_color="#495057",
                container=container,
            )
            odr_set_btn.do_onclick(lambda ch=ch: self._on_set_odr(ch))
            self._odr_set_btns[ch] = odr_set_btn

            # Mode selector — 3 horizontally-aligned buttons; selected has a
            # colored bg, unselected is neutral gray.
            btns_this_ch = []
            for mi, mlabel in enumerate(_MODE_LABELS):
                if mlabel == "OFF":
                    color, press = _MODE_COLORS[mlabel]
                else:
                    color, press = _MODE_UNSEL_COLOR, _MODE_UNSEL_PRESS
                # Center the 3 buttons horizontally over the params box below
                # (which is 310 wide). Buttons: 3*80 + 2*15 = 270 → offset 20.
                mbtn = StyledButton(
                    mlabel, f"mode_btn_{ch}_{mi}",
                    x + 20 + mi * 95, _Y_BASE + 45, width=80, height=32,
                    normal_color=color, press_color=press,
                    container=container,
                )
                mbtn.do_onclick(lambda ch=ch, m=mlabel: self._select_mode(ch, m))
                btns_this_ch.append(mbtn)
            self._mode_btns[ch] = btns_this_ch

            # STEADY params container — grew to 165 tall to fit the meter compare
            # row. Centered between mode buttons (end _Y_BASE+77) and Apply/Stop
            # (start _Y_BASE+265): gap 188, container 165 → top offset ~11.
            steady_c = StyledContainer(
                f"steady_c_{ch}", x, _Y_BASE + 88, 310, 165,
                border=False, bg_color=False,
                position="absolute",
                container=container,
            )
            steady_c.style["display"] = "none"
            self._steady_containers[ch] = steady_c

            # Voltage row: label, input, unit (V), then a paired -/+ over on
            # the right side. The −/+ pair sits closer together than the unit
            # sits to the −, so it reads as a single stepper.
            StyledLabel("Voltage:", f"lbl_v_{ch}", 10, 10, width=70, height=24,
                        color="#444", flex=True, justify_content="flex-start",
                        container=steady_c)
            v_inp = StyledTextInput(f"v_inp_{ch}", 85, 10, width=55, height=24,
                                    text="0.0", container=steady_c)
            v_inp.style["box-sizing"] = "border-box"
            self._voltage_inputs[ch] = v_inp

            StyledLabel("V", f"lbl_v_unit_{ch}", 148, 10, width=15, height=24,
                        color="#444", flex=True, justify_content="flex-start",
                        container=steady_c)

            v_minus_btn = StyledButton(
                "−", f"v_minus_{ch}", 210, 7, width=40, height=30,
                normal_color="#6c757d", press_color="#495057",
                container=steady_c,
            )
            v_minus_btn.style["outline"] = "none"
            v_minus_btn.do_onclick(lambda ch=ch: self._on_voltage_step(ch, -0.05))

            v_plus_btn = StyledButton(
                "+", f"v_plus_{ch}", 254, 7, width=40, height=30,
                normal_color="#6c757d", press_color="#495057",
                container=steady_c,
            )
            v_plus_btn.style["outline"] = "none"
            v_plus_btn.do_onclick(lambda ch=ch: self._on_voltage_step(ch, +0.05))

            # Timer row — wrap checkbox + label + dur + unit in a flex container
            # so the checkbox and "Timer" text share a common vertical center.
            timer_row_c = StyledContainer(
                f"timer_row_c_{ch}", 6, 48, 300, 30,
                border=False, bg_color=False,
                position="absolute",
                container=steady_c,
            )
            timer_row_c.style.update({
                "display": "flex",
                "align-items": "center",
            })

            timer_cb = gui.CheckBox(checked=False)
            timer_cb.style.update({
                "margin": "0",
                "width": "16px",
                "height": "16px",
                "flex": "0 0 auto",
            })
            timer_row_c.append(timer_cb, f"timer_cb_{ch}")
            self._timer_checkboxes[ch] = timer_cb

            timer_lbl = gui.Label("Timer")
            timer_lbl.style.update({
                "color": "#444",
                "font-size": "15px",
                "margin-left": "6px",
                "flex": "0 0 auto",
            })
            timer_row_c.append(timer_lbl, f"timer_lbl_{ch}")

            dur_inp = StyledTextInput(f"dur_inp_{ch}", 0, 0, width=45, height=24,
                                      text="10")
            dur_inp.style["position"] = "relative"
            dur_inp.style["left"] = "0"
            dur_inp.style["top"] = "0"
            dur_inp.style["margin-left"] = "16px"
            dur_inp.style["display"] = "none"
            dur_inp.style["flex"] = "0 0 auto"
            timer_row_c.append(dur_inp, f"dur_inp_{ch}")
            self._duration_inputs[ch] = dur_inp

            tunit_dd = StyledDropDown("Min", f"tunit_dd_{ch}", 0, 0, width=75, height=24)
            tunit_dd.append("Hour")
            tunit_dd.append("Day")
            tunit_dd.append("Month")
            tunit_dd.style["position"] = "relative"
            tunit_dd.style["left"] = "0"
            tunit_dd.style["top"] = "0"
            tunit_dd.style["margin-left"] = "8px"
            tunit_dd.style["display"] = "none"
            tunit_dd.style["flex"] = "0 0 auto"
            timer_row_c.append(tunit_dd, f"tunit_dd_{ch}")
            self._time_unit_dds[ch] = tunit_dd

            timer_cb.onchange.do(
                lambda emitter, value, ch=ch: self._on_timer_toggle(ch, value)
            )

            # meter-compare row — checkbox + "Meter" label + Poll interval field.
            # Only one channel can compare against the meter at a time (mutex
            # enforced in _on_meter_compare_toggle).
            meter_row_c = StyledContainer(
                f"meter_row_c_{ch}", 6, 85, 300, 30,
                border=False, bg_color=False,
                position="absolute",
                container=steady_c,
            )
            meter_row_c.style.update({
                "display": "flex",
                "align-items": "center",
            })

            meter_cb = gui.CheckBox(checked=False)
            meter_cb.style.update({
                "margin": "0",
                "width": "16px",
                "height": "16px",
                "flex": "0 0 auto",
            })
            meter_row_c.append(meter_cb, f"meter_cb_{ch}")
            self._meter_compare_cbs[ch] = meter_cb

            meter_lbl = gui.Label("Meter")
            meter_lbl.style.update({
                "color": "#444",
                "font-size": "15px",
                "margin-left": "6px",
                "flex": "0 0 auto",
            })
            meter_row_c.append(meter_lbl, f"meter_row_lbl_{ch}")

            poll_lbl = gui.Label("Poll:")
            poll_lbl.style.update({
                "color": "#444",
                "font-size": "15px",
                "margin-left": "16px",
                "flex": "0 0 auto",
            })
            meter_row_c.append(poll_lbl, f"poll_lbl_{ch}")

            poll_inp = StyledTextInput(f"poll_inp_{ch}", 0, 0, width=55, height=24,
                                       text="500")
            poll_inp.style["position"] = "relative"
            poll_inp.style["left"] = "0"
            poll_inp.style["top"] = "0"
            poll_inp.style["margin-left"] = "6px"
            poll_inp.style["flex"] = "0 0 auto"
            meter_row_c.append(poll_inp, f"poll_inp_{ch}")
            self._meter_poll_inputs[ch] = poll_inp

            poll_unit_lbl = gui.Label("ms")
            poll_unit_lbl.style.update({
                "color": "#444",
                "font-size": "15px",
                "margin-left": "6px",
                "flex": "0 0 auto",
            })
            meter_row_c.append(poll_unit_lbl, f"poll_unit_lbl_{ch}")

            meter_cb.onchange.do(
                lambda emitter, value, ch=ch: self._on_meter_compare_toggle(ch, value)
            )

            # STEADY live readout — INSIDE the steady_c box, below meter row
            readout = StyledLabel(
                "Set: —   |   Measured: —", f"steady_readout_{ch}",
                10, 125, width=290, height=30,
                color="#333",
                container=steady_c,
            )
            readout.style["display"] = "none"
            readout.style["font-family"] = "monospace"
            readout.style["font-size"] = "14px"
            readout.style["font-weight"] = "bold"
            readout.style["padding-top"] = "4px"
            self._steady_readouts[ch] = readout

            # SWEEP params container — vertically centered between mode buttons
            # and Apply/Stop row.
            sweep_c = StyledContainer(
                f"sweep_c_{ch}", x, _Y_BASE + 131, 310, 80,
                border=False, bg_color=False,
                position="absolute",
                container=container,
            )
            sweep_c.style["display"] = "none"
            self._sweep_containers[ch] = sweep_c

            # Sweep rows: labels left-aligned to match STEADY's "Voltage:"
            # style; inputs share x=115 so the Range/Step columns line up.
            # Row Ys 13/43 center the 54-tall content in the 80-tall container.
            StyledLabel("Range:", f"lbl_range_{ch}", 10, 13, width=100, height=24,
                        color="#444", flex=True, justify_content="flex-start",
                        container=sweep_c)
            range_inp = StyledTextInput(f"range_inp_{ch}", 115, 13, width=60, height=24,
                                        text="1.0", container=sweep_c)
            range_inp.style["box-sizing"] = "border-box"
            self._range_inputs[ch] = range_inp
            StyledLabel("V", f"lbl_range_unit_{ch}", 183, 13, width=15, height=24,
                        color="#444", flex=True, justify_content="flex-start",
                        container=sweep_c)

            StyledLabel("Step size:", f"lbl_step_{ch}", 10, 43, width=100, height=24,
                        color="#444", flex=True, justify_content="flex-start",
                        container=sweep_c)
            step_inp = StyledTextInput(f"step_inp_{ch}", 115, 43, width=60, height=24,
                                       text="10", container=sweep_c)
            step_inp.style["box-sizing"] = "border-box"
            self._step_inputs[ch] = step_inp
            StyledLabel("mV", f"lbl_step_unit_{ch}", 183, 43, width=25, height=24,
                        color="#444", flex=True, justify_content="flex-start",
                        container=sweep_c)

            # Apply + Stop buttons — taller for easier clicking
            apply_btn = StyledButton(
                "Apply", f"apply_btn_{ch}",
                x, _Y_BASE + 265, width=150, height=40,
                normal_color="#28a745", press_color="#1e7e34",
                container=container,
            )
            apply_btn.do_onclick(lambda ch=ch: self._on_apply(ch))
            self._apply_btns[ch] = apply_btn

            stop_btn = StyledButton(
                "Stop", f"stop_btn_{ch}",
                x + 160, _Y_BASE + 265, width=150, height=40,
                normal_color="#dc3545", press_color="#a71d2a",
                container=container,
            )
            stop_btn.do_onclick(lambda ch=ch: self._on_stop(ch))

        # ---- Log terminal (below channels) — centered in the 1400-wide root ----
        _LOG_W = 800
        _LOG_X = (_W - _LOG_W) // 2
        _LOG_Y = _Y_BASE + 320

        self._log_text = gui.TextInput(singleline=False)
        self._log_text.attributes["readonly"] = "true"
        self._log_text.css_position = "absolute"
        self._log_text.css_left = f"{_LOG_X}px"
        self._log_text.css_top = f"{_LOG_Y}px"
        self._log_text.css_width = f"{_LOG_W}px"
        self._log_text.css_height = "300px"
        self._log_text.style.update({
            "border": "1px solid #444",
            "background-color": "#1e1e1e",
            "color": "#f0f0f0",
            "font-family": "monospace",
            "font-size": "13px",
            "padding": "10px",
            "border-radius": "6px",
            "box-shadow": "0 0 6px rgba(0,0,0,0.3)",
            "overflow-y": "auto",
            "white-space": "pre-wrap",
        })
        container.append(self._log_text, "log_text")

        # Refresh button — bottom-right just under the terminal; clears the log.
        _RB_W, _RB_H = 90, 30
        refresh_log_btn = StyledButton(
            "Refresh", "refresh_log_btn",
            _LOG_X + _LOG_W - _RB_W, _LOG_Y + 340,
            width=_RB_W, height=_RB_H,
            normal_color="#6c757d", press_color="#495057",
            container=container,
        )
        refresh_log_btn.do_onclick(self._on_refresh_log)

    # ------------------------------------------------------------------
    # Tab 2 — Channels panel (per-channel plots)
    # ------------------------------------------------------------------

    def _build_channels_panel(self, container):
        _PLOT_COL_X = [5, 645]
        _PLOT_ROW_Y = [30, 510]
        _PLOT_W = 620
        _PLOT_H = 430

        for ch in range(4):
            col = ch % 2
            row = ch // 2
            x = _PLOT_COL_X[col]
            y = _PLOT_ROW_Y[row]
            self._build_plot_column(ch, container, x, y, _PLOT_W, _PLOT_H)

    def _build_plot_column(self, ch: int, container, x: int, y: int,
                           plot_w: int = 675, plot_h: int = 430):
        # "Ch N" bold label
        StyledLabel(
            f"Ch {ch}", f"plot_lbl_ch_{ch}",
            x + 5, y, width=100, height=22,
            color="#222", bold=True,
            container=container,
        )

        # Mode indicator label
        mode_ind = StyledLabel(
            "OFF", f"mode_ind_{ch}",
            x + 70, y, width=200, height=22,
            color="#555",
            container=container,
        )
        self._mode_indicator_labels[ch] = mode_ind

        # Plot image container
        plot_c = StyledContainer(
            f"plot_c_{ch}", x, y + 26, plot_w, plot_h,
            border=False, bg_color=False,
            position="absolute",
            container=container,
        )
        plot_c.style["display"] = "none"
        self._plot_containers[ch] = plot_c

        # Plot image inside container
        plot_img = gui.Image("")
        plot_img.css_position = "absolute"
        plot_img.css_left = "0px"
        plot_img.css_top = "0px"
        plot_img.css_width = f"{plot_w - 5}px"
        plot_img.css_height = f"{plot_h - 5}px"
        plot_c.append(plot_img, f"plot_img_{ch}")
        self._plot_imgs[ch] = plot_img

        # "No plot — select SWEEP" label
        no_plot_lbl = StyledLabel(
            "No plot — select SWEEP", f"no_plot_lbl_{ch}",
            x + 5, y + 26, width=plot_w - 10, height=22,
            color="#aaa",
            container=container,
        )
        self._no_plot_labels[ch] = no_plot_lbl

    # ------------------------------------------------------------------
    # Tab 3 — Time-vs-current panel (per-channel live plots)
    # ------------------------------------------------------------------

    def _build_time_panel(self, container):
        _PLOT_COL_X = [5, 645]
        _PLOT_ROW_Y = [30, 510]
        _PLOT_W = 620
        _PLOT_H = 430

        for ch in range(4):
            col = ch % 2
            row = ch // 2
            x = _PLOT_COL_X[col]
            y = _PLOT_ROW_Y[row]
            self._build_time_column(ch, container, x, y, _PLOT_W, _PLOT_H)

    def _build_time_column(self, ch: int, container, x: int, y: int,
                           plot_w: int = 620, plot_h: int = 430):
        StyledLabel(
            f"Ch {ch}", f"time_lbl_ch_{ch}",
            x + 5, y, width=100, height=22,
            color="#222", bold=True,
            container=container,
        )

        plot_c = StyledContainer(
            f"time_plot_c_{ch}", x, y + 26, plot_w, plot_h,
            border=False, bg_color=False,
            position="absolute",
            container=container,
        )
        self._time_plot_containers[ch] = plot_c

        plot_img = gui.Image("")
        plot_img.css_position = "absolute"
        plot_img.css_left = "0px"
        plot_img.css_top = "0px"
        plot_img.css_width = f"{plot_w - 5}px"
        plot_img.css_height = f"{plot_h - 5}px"
        plot_c.append(plot_img, f"time_plot_img_{ch}")
        self._time_plot_imgs[ch] = plot_img

        no_plot_lbl = StyledLabel(
            "No data yet", f"no_time_plot_lbl_{ch}",
            x + 5, y + 26, width=plot_w - 10, height=22,
            color="#aaa",
            container=container,
        )
        self._no_time_plot_labels[ch] = no_plot_lbl

    # ------------------------------------------------------------------
    # Tab 4 — Calibration panel (PCB ADC vs meter scatter per channel)
    # ------------------------------------------------------------------

    def _build_calib_panel(self, container):
        # --- Calibration values table (top of panel) -----------------------
        # One row per channel: R_1k, R_gain, DAC Vref inputs + Recalibrate btn.
        # Firmware side lives in voltage-source/components/app/channel.cpp:
        # set_calibration() — it validates against R_1K_TOL / R_GAIN_TOL /
        # DAC_VREF_TOL and persists to NVS on success.
        _TABLE_TITLE_Y = 5
        _HEADER_Y = 30
        _ROW_Y0 = 60
        _ROW_H = 34

        # Column X positions — centered in the 1430-wide panel.
        _COL_CH_X = 380
        _COL_R1K_X = 470
        _COL_RGAIN_X = 640
        _COL_VREF_X = 810
        _COL_BTN_X = 970
        _INP_W = 130

        StyledLabel(
            "Calibration values", "calib_table_title",
            _COL_CH_X, _TABLE_TITLE_Y, width=300, height=22,
            color="#222", bold=True,
            flex=True, justify_content="flex-start",
            container=container,
        )

        def _hdr(text, ident, x, w):
            StyledLabel(
                text, ident, x, _HEADER_Y, width=w, height=22,
                color="#555", bold=True,
                flex=True, justify_content="flex-start",
                container=container,
            )

        _hdr("Channel", "calib_hdr_ch", _COL_CH_X, 70)
        _hdr("R_1k (Ω)", "calib_hdr_r1k", _COL_R1K_X, _INP_W)
        _hdr("R_gain (Ω)", "calib_hdr_rgain", _COL_RGAIN_X, _INP_W)
        _hdr("DAC Vref (V)", "calib_hdr_vref", _COL_VREF_X, _INP_W)

        for ch in range(4):
            y = _ROW_Y0 + ch * _ROW_H
            StyledLabel(
                f"Ch {ch}", f"calib_row_lbl_{ch}",
                _COL_CH_X, y, width=70, height=26,
                color="#222", bold=True,
                flex=True, justify_content="flex-start",
                container=container,
            )
            r1k_inp = StyledTextInput(
                f"cal_r1k_inp_{ch}", _COL_R1K_X, y, width=_INP_W, height=26,
                text=_CAL_R1K_DEFAULT, container=container,
            )
            r1k_inp.style["box-sizing"] = "border-box"
            self._cal_r1k_inputs[ch] = r1k_inp

            rgain_inp = StyledTextInput(
                f"cal_rgain_inp_{ch}", _COL_RGAIN_X, y, width=_INP_W, height=26,
                text=_CAL_RGAIN_DEFAULT, container=container,
            )
            rgain_inp.style["box-sizing"] = "border-box"
            self._cal_rgain_inputs[ch] = rgain_inp

            vref_inp = StyledTextInput(
                f"cal_vref_inp_{ch}", _COL_VREF_X, y, width=_INP_W, height=26,
                text=_CAL_VREF_DEFAULT, container=container,
            )
            vref_inp.style["box-sizing"] = "border-box"
            self._cal_vref_inputs[ch] = vref_inp

            recal_btn = StyledButton(
                "Recalibrate", f"recal_btn_{ch}",
                _COL_BTN_X, y - 2, width=140, height=30,
                normal_color="#28a745", press_color="#1e7e34",
                container=container,
            )
            recal_btn.do_onclick(lambda ch=ch: self._on_recalibrate(ch))
            self._recalibrate_btns[ch] = recal_btn

        # --- Per-channel PCB-vs-meter scatter plots (below the table) --------
        _PLOT_COL_X = [5, 645]
        _PLOT_ROW_Y = [230, 640]
        _PLOT_W = 620
        _PLOT_H = 380

        for ch in range(4):
            col = ch % 2
            row = ch // 2
            x = _PLOT_COL_X[col]
            y = _PLOT_ROW_Y[row]
            self._build_calib_column(ch, container, x, y, _PLOT_W, _PLOT_H)

    def _build_calib_column(self, ch: int, container, x: int, y: int,
                            plot_w: int = 620, plot_h: int = 430):
        StyledLabel(
            f"Ch {ch}", f"calib_lbl_ch_{ch}",
            x + 5, y, width=100, height=22,
            color="#222", bold=True,
            container=container,
        )

        plot_c = StyledContainer(
            f"calib_plot_c_{ch}", x, y + 26, plot_w, plot_h,
            border=False, bg_color=False,
            position="absolute",
            container=container,
        )
        self._calib_plot_containers[ch] = plot_c

        plot_img = gui.Image("")
        plot_img.css_position = "absolute"
        plot_img.css_left = "0px"
        plot_img.css_top = "0px"
        plot_img.css_width = f"{plot_w - 5}px"
        plot_img.css_height = f"{plot_h - 5}px"
        plot_c.append(plot_img, f"calib_plot_img_{ch}")
        self._calib_plot_imgs[ch] = plot_img

        no_plot_lbl = StyledLabel(
            "No compare data yet", f"no_calib_plot_lbl_{ch}",
            x + 5, y + 26, width=plot_w - 10, height=22,
            color="#aaa",
            container=container,
        )
        self._no_calib_plot_labels[ch] = no_plot_lbl

    # ------------------------------------------------------------------
    # Tab switching
    # ------------------------------------------------------------------

    def _switch_tab(self, idx: int):
        for i, panel in enumerate(self._panels):
            panel.style["display"] = "block" if i == idx else "none"
        for i, btn in enumerate(self._tab_btns):
            color = "#007BFF" if i == idx else "#6c757d"
            btn.style["background-color"] = color
            btn.normal_color = color
        self._active_tab = idx

    def _update_extra_tabs_visibility(self):
        """All tabs are always visible now (the meter sweep tab was removed).
        Kept as a hook so idle() can call it without a NameError."""
        return

    # ------------------------------------------------------------------
    # Mode visibility
    # ------------------------------------------------------------------

    def _on_voltage_step(self, ch: int, delta: float):
        """± button for STEADY voltage: bump the DOM value by delta and clamp.

        Runs in the browser so the visible input always reflects the change even
        when the field has focus (remi's server->client diff can otherwise
        swallow same-shape updates). Dispatches a `change` event so remi syncs
        the new value back to the server for the next Apply.
        """
        eid = self._voltage_inputs[ch].identifier
        js = (
            "(function(){"
            f"var e=document.getElementById('{eid}');"
            "if(!e)return;"
            "var v=parseFloat(e.value);if(isNaN(v))v=0;"
            f"v=Math.max({_V_MIN},Math.min({_V_MAX},v+({delta})));"
            "v=Math.round(v*1000)/1000;"
            "e.value=v.toString();"
            "e.dispatchEvent(new Event('change',{bubbles:true}));"
            "e.dispatchEvent(new Event('input',{bubbles:true}));"
            "})();"
        )
        self.execute_javascript(js)

    def _on_timer_toggle(self, ch: int, value):
        enabled = bool(value)
        display = "block" if enabled else "none"
        for w in (self._duration_inputs[ch], self._time_unit_dds[ch]):
            if w is not None:
                w.style["display"] = display

    def _select_mode(self, ch: int, mode: str):
        """User picked a mode button. Update button colors, param visibility,
        Plots-tab visibility, and mode indicator. Does not send anything to
        the ESP — Apply is what actually pushes the command."""
        self._selected_modes[ch] = mode

        # Recolor the 3 mode buttons for this channel
        btns = self._mode_btns[ch] or []
        for mi, mlabel in enumerate(_MODE_LABELS):
            if mi >= len(btns) or btns[mi] is None:
                continue
            if mlabel == mode:
                color, press = _MODE_COLORS[mlabel]
            else:
                color, press = _MODE_UNSEL_COLOR, _MODE_UNSEL_PRESS
            btns[mi].style["background-color"] = color
            btns[mi].normal_color = color
            btns[mi].press_color = press

        # Show/hide STEADY and SWEEP param containers
        if mode == "STEADY":
            self._steady_containers[ch].style["display"] = "block"
            self._sweep_containers[ch].style["display"] = "none"
        elif mode == "SWEEP":
            self._steady_containers[ch].style["display"] = "none"
            self._sweep_containers[ch].style["display"] = "block"
        else:  # OFF
            self._steady_containers[ch].style["display"] = "none"
            self._sweep_containers[ch].style["display"] = "none"

        # Plots tab: show plot only for SWEEP
        if mode == "SWEEP":
            if self._plot_containers[ch] is not None:
                self._plot_containers[ch].style["display"] = "block"
            if self._no_plot_labels[ch] is not None:
                self._no_plot_labels[ch].style["display"] = "none"
        else:
            if self._plot_containers[ch] is not None:
                self._plot_containers[ch].style["display"] = "none"
            if self._no_plot_labels[ch] is not None:
                self._no_plot_labels[ch].style["display"] = "block"

        # Mode indicator (firmware-truth may overwrite this on next data tick)
        if self._mode_indicator_labels[ch] is not None:
            self._mode_indicator_labels[ch].set_text(mode)

    # ------------------------------------------------------------------
    # Idle polling
    # ------------------------------------------------------------------

    def idle(self):
        # One-shot: inject a style rule that suppresses the browser focus
        # outline on clicked buttons / focused inputs (the "dotted border").
        if not self._css_injected:
            try:
                self.execute_javascript(
                    "if(!document.getElementById('_vs_no_outline')){"
                    "var s=document.createElement('style');"
                    "s.id='_vs_no_outline';"
                    "s.textContent='button:focus,button:focus-visible,input:focus,select:focus,textarea:focus{outline:none !important;}';"
                    "document.head.appendChild(s);"
                    "}"
                )
                self._css_injected = True
            except Exception:
                pass

        connected = _vs.is_connected

        # Connection state change
        if connected != self._was_connected:
            if connected:
                port_name = _vs.connected_port
                self._status_label.set_text(f"●  {port_name}")
                self._status_label.style["color"] = "#28a745"
                self._connect_btn.set_text("Disconnect")
                self._connect_btn.style["background-color"] = "#dc3545"
                self._connect_btn.normal_color = "#dc3545"
                self._connect_btn.press_color = "#a71d2a"
                threading.Thread(
                    target=_vs.start_reading,
                    kwargs={"output_dir": "output"},
                    daemon=True,
                ).start()
                _vs_log(f"[Connected] {port_name}")
            else:
                self._status_label.set_text("●  Disconnected")
                self._status_label.style["color"] = "#dc3545"
                self._connect_btn.set_text("Connect")
                self._connect_btn.style["background-color"] = "#28a745"
                self._connect_btn.normal_color = "#28a745"
                self._connect_btn.press_color = "#1e7e34"
                _vs.stop_reading()
                _vs_log("[Disconnected]")
                # Reset every channel to OFF: safest UI state when we've lost
                # comms with the ESP; user must explicitly re-Apply after reconnect.
                for ch in range(4):
                    self._select_mode(ch, "OFF")
                    self._sweep_active[ch] = False
                    self._active_modes[ch] = "OFF"
                    dot = self._status_dots[ch]
                    if dot is not None:
                        dot.style["color"] = _DOT_OFF_COLOR
                    readout = self._steady_readouts[ch]
                    if readout is not None:
                        readout.style["display"] = "none"
            self._was_connected = connected

        # meter connection state change
        meter_connected = _meter_is_connected()
        if meter_connected != self._meter_was_connected:
            if meter_connected:
                self._meter_status_label.set_text(f"●  {_meter.connected_resource}")
                self._meter_status_label.style["color"] = "#28a745"
                self._meter_connect_btn.set_text("Disconnect")
                self._meter_connect_btn.style["background-color"] = "#dc3545"
                self._meter_connect_btn.normal_color = "#dc3545"
                self._meter_connect_btn.press_color = "#a71d2a"
            else:
                self._meter_status_label.set_text("●  Disconnected")
                self._meter_status_label.style["color"] = "#dc3545"
                self._meter_connect_btn.set_text("Connect")
                self._meter_connect_btn.style["background-color"] = "#28a745"
                self._meter_connect_btn.normal_color = "#28a745"
                self._meter_connect_btn.press_color = "#1e7e34"
                # Any in-flight compare threads should stop on meter loss
                for ch in range(4):
                    self._stop_meter_compare(ch)
            self._meter_was_connected = meter_connected

        # Show/hide Sweep + Calibration tabs based on connection state.
        self._update_extra_tabs_visibility()

        # Per-channel data polling — firmware's mode field drives dot/readout truth
        now = time.time()
        for ch in range(4):
            if not _vs.has_new_data(ch):
                continue
            data = _vs.get_channel_data(ch)
            if not data:
                continue
            latest = data[-1]
            # Firmware enum (task_comms.hpp Mode):
            #   0=SWEEP, 1=STEADY, 2=IDLE, 3=CALIBRATION, 4=ODR.
            # Data records only carry SWEEP/STEADY (running) or IDLE (stop);
            # CALIBRATION/ODR are transient commands that don't update the
            # channel's runtime mode. Treat anything >= 2 as OFF for the UI.
            if latest.mode >= 2:
                fw_str = "OFF"
            elif latest.mode == 0:
                fw_str = "SWEEP"
            else:
                fw_str = "STEADY"

            # Dot color follows firmware truth
            dot = self._status_dots[ch]
            if dot is not None:
                dot.style["color"] = _DOT_OFF_COLOR if fw_str == "OFF" else _DOT_ON_COLOR
            self._active_modes[ch] = fw_str

            if self._mode_indicator_labels[ch] is not None:
                self._mode_indicator_labels[ch].set_text(fw_str)

            readout = self._steady_readouts[ch]
            if fw_str == "SWEEP":
                # Mark sweep running; wait until it ends before drawing (avoids flash)
                self._sweep_active[ch] = True
                if readout is not None:
                    readout.style["display"] = "none"
            elif fw_str == "STEADY":
                if readout is not None:
                    readout.style["display"] = "block"
                    meter_str = ""
                    meter_samples = self._meter_compare_data.get(ch) or []
                    if meter_samples:
                        # tuple: (elapsed_s, pcb_uA, meter_uA)
                        meter_i_uA = meter_samples[-1][2]
                        meter_str = f"   |   Meter: {meter_i_uA:.3f} µA"
                    readout.set_text(
                        f"Set: {self._applied_voltages[ch]:.3f} V   |   "
                        f"Measured: {latest.current:.5f} µA{meter_str}"
                    )
            else:  # OFF
                if readout is not None:
                    readout.style["display"] = "none"
                if self._sweep_active[ch]:
                    self._update_sweep_plot(ch, data)
                    self._sweep_active[ch] = False

            # Time-vs-current live plot — throttle to 1 Hz per channel so
            # matplotlib rendering doesn't starve the idle loop. Calibration
            # plot piggy-backs on the same throttle.
            if now - self._last_time_render[ch] >= 1.0:
                self._update_time_plot(ch, data)
                self._update_calib_plot(ch)
                self._last_time_render[ch] = now

        # Update log text area (throttle: only when new lines have been written)
        if _log_seq != self._last_log_seq and self._log_text is not None:
            self._log_text.set_text("\n".join(_log_lines))
            self._last_log_seq = _log_seq
            eid = self._log_text.identifier
            self.execute_javascript(
                f"setTimeout(function(){{"
                f"var e=document.getElementById('{eid}');"
                f"if(e)e.scrollTop=e.scrollHeight;"
                f"}},30);"
            )

    # ------------------------------------------------------------------
    # Per-channel sweep plot
    # ------------------------------------------------------------------

    def _update_sweep_plot(self, ch: int, data):
        """Plot a single channel sweep (I vs V) in the Channels tab."""
        try:
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_agg import FigureCanvasAgg
            from matplotlib.ticker import MaxNLocator, AutoMinorLocator

            sweep = [d for d in data if d.mode == 0]
            if len(sweep) < 2:
                return

            colors = {0: "#007BFF", 1: "#28a745", 2: "#dc3545", 3: "#fd7e14"}

            fig = Figure(figsize=(6.5, 4.1), dpi=100)
            canvas = FigureCanvasAgg(fig)
            ax = fig.add_subplot(111)

            voltages = [d.voltage for d in sweep]   
            currents = [d.current for d in sweep]   
            ax.plot(voltages, currents, color=colors[ch], linewidth=1.2)

            ax.set_xlabel("Voltage (V)")
            ax.set_ylabel("Current (µA)")

            # Consistent grid across all channels: ~8 major ticks per axis,
            # 5-way minor subdivisions with a lighter minor grid (no labels).
            nice_steps = [1, 2, 2.5, 5, 10]
            ax.xaxis.set_major_locator(MaxNLocator(nbins=8, steps=nice_steps))
            ax.yaxis.set_major_locator(MaxNLocator(nbins=8, steps=nice_steps))
            ax.xaxis.set_minor_locator(AutoMinorLocator(5))
            ax.yaxis.set_minor_locator(AutoMinorLocator(5))
            ax.grid(True, which="major", alpha=0.4, linewidth=0.8)
            ax.grid(True, which="minor", alpha=0.15, linewidth=0.5)

            fig.tight_layout()

            buf = io.BytesIO()
            canvas.print_png(buf)
            uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
            self._plot_imgs[ch].attributes["src"] = uri
        except Exception as e:
            print(f"[VoltageSource] Sweep plot Ch{ch} error: {e}", flush=True)

    def _update_time_plot(self, ch: int, data):
        """Plot current vs elapsed time for a channel in the Time tab.

        Ticks are rendered in whichever unit (s/min/h/d) keeps the axis readable
        for the current data span."""
        try:
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_agg import FigureCanvasAgg
            from matplotlib.ticker import MaxNLocator, AutoMinorLocator

            if len(data) < 2:
                return

            t0 = data[0].time_s
            elapsed_s = [d.time_s - t0 for d in data]
            currents = [d.current for d in data]

            span_s = elapsed_s[-1] - elapsed_s[0]
            if span_s < 60:
                unit, div = "s", 1.0
            elif span_s < 3600:
                unit, div = "min", 60.0
            elif span_s < 86400:
                unit, div = "h", 3600.0
            else:
                unit, div = "d", 86400.0
            x_scaled = [t / div for t in elapsed_s]

            colors = {0: "#007BFF", 1: "#28a745", 2: "#dc3545", 3: "#fd7e14"}

            fig = Figure(figsize=(6.5, 4.1), dpi=100)
            canvas = FigureCanvasAgg(fig)
            ax = fig.add_subplot(111)
            ax.plot(x_scaled, currents, color=colors[ch], linewidth=1.2, label="PCB")

            # Overlay meter compare data (if any). Meter samples arrive on their
            # own thread with time.time() timestamps; we assume Apply STEADY
            # kicked off both streams roughly together, so the two elapsed
            # timelines align well enough for visual comparison.
            # tuple format: (elapsed_s, pcb_uA, meter_uA) — index 2 is Meter µA.
            meter_samples = list(self._meter_compare_data.get(ch) or [])
            if len(meter_samples) >= 2:
                meter_t = [s[0] / div for s in meter_samples]
                meter_i_uA = [s[2] for s in meter_samples]
                ax.plot(meter_t, meter_i_uA, color="#000000", linewidth=1.0,
                        linestyle="--", label="meter")
                ax.legend(loc="best", fontsize=8)

            ax.set_xlabel(f"Time ({unit})")
            ax.set_ylabel("Current (µA)")

            nice_steps = [1, 2, 2.5, 5, 10]
            ax.xaxis.set_major_locator(MaxNLocator(nbins=8, steps=nice_steps))
            ax.yaxis.set_major_locator(MaxNLocator(nbins=8, steps=nice_steps))
            ax.xaxis.set_minor_locator(AutoMinorLocator(5))
            ax.yaxis.set_minor_locator(AutoMinorLocator(5))
            ax.grid(True, which="major", alpha=0.4, linewidth=0.8)
            ax.grid(True, which="minor", alpha=0.15, linewidth=0.5)

            fig.tight_layout()

            buf = io.BytesIO()
            canvas.print_png(buf)
            uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
            self._time_plot_imgs[ch].attributes["src"] = uri

            if self._no_time_plot_labels[ch] is not None:
                self._no_time_plot_labels[ch].style["display"] = "none"
        except Exception as e:
            print(f"[VoltageSource] Time plot Ch{ch} error: {e}", flush=True)

    def _update_calib_plot(self, ch: int):
        """Scatter of PCB ADC current (x) vs meter current (y), one point per
        meter compare sample. Draws a y=x reference line so deviation from
        ideal calibration is obvious at a glance."""
        try:
            samples = list(self._meter_compare_data.get(ch) or [])
            if len(samples) < 2:
                return

            from matplotlib.figure import Figure
            from matplotlib.backends.backend_agg import FigureCanvasAgg
            from matplotlib.ticker import MaxNLocator, AutoMinorLocator

            # tuple: (elapsed_s, pcb_uA, meter_uA)
            pcb_x = [s[1] for s in samples]
            meter_y = [s[2] for s in samples]

            colors = {0: "#007BFF", 1: "#28a745", 2: "#dc3545", 3: "#fd7e14"}

            fig = Figure(figsize=(6.5, 4.1), dpi=100)
            canvas = FigureCanvasAgg(fig)
            ax = fig.add_subplot(111)
            ax.scatter(pcb_x, meter_y, s=18, color=colors[ch],
                       edgecolors="none", alpha=0.8)

            # y = x reference line spanning the union of both axes
            lo = min(min(pcb_x), min(meter_y))
            hi = max(max(pcb_x), max(meter_y))
            if lo == hi:
                lo, hi = lo - 1, hi + 1
            ax.plot([lo, hi], [lo, hi], color="#666", linestyle="--",
                    linewidth=0.8, label="y = x")
            ax.legend(loc="best", fontsize=8)

            ax.set_xlabel("PCB current (µA)")
            ax.set_ylabel("meter current (µA)")
            ax.set_title(f"Ch {ch}: PCB vs meter  ({len(samples)} samples)",
                         fontsize=10)

            nice_steps = [1, 2, 2.5, 5, 10]
            ax.xaxis.set_major_locator(MaxNLocator(nbins=8, steps=nice_steps))
            ax.yaxis.set_major_locator(MaxNLocator(nbins=8, steps=nice_steps))
            ax.xaxis.set_minor_locator(AutoMinorLocator(5))
            ax.yaxis.set_minor_locator(AutoMinorLocator(5))
            ax.grid(True, which="major", alpha=0.4, linewidth=0.8)
            ax.grid(True, which="minor", alpha=0.15, linewidth=0.5)

            fig.tight_layout()

            buf = io.BytesIO()
            canvas.print_png(buf)
            uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
            self._calib_plot_imgs[ch].attributes["src"] = uri

            if self._no_calib_plot_labels[ch] is not None:
                self._no_calib_plot_labels[ch].style["display"] = "none"
        except Exception as e:
            print(f"[VoltageSource] Calib plot Ch{ch} error: {e}", flush=True)

    # ------------------------------------------------------------------
    # Channel callbacks
    # ------------------------------------------------------------------

    def _timer_duration_seconds(self, ch: int):
        """Return the STEADY timer duration in seconds, or None if timer is off
        (Inf) or params are invalid. Used to bound the meter compare run."""
        try:
            cb = self._timer_checkboxes[ch]
            if cb is None or not cb.get_value():
                return None
            raw = float(self._duration_inputs[ch].get_value() or "0")
            unit = self._time_unit_dds[ch].get_value() or "Min"
        except (ValueError, AttributeError):
            return None
        factor = {"Min": 60.0, "Hour": 3600.0, "Day": 86400.0, "Month": 2_592_000.0}
        return raw * factor.get(unit, 60.0)

    def _on_apply(self, ch: int):
        if not _vs.is_connected:
            _vs_log(f"[Apply Ch {ch}] Not connected")
            return
        mode = self._selected_modes[ch]
        # Any Apply cancels a running meter compare for this channel — modes
        # other than STEADY definitely can't have one, and re-applying STEADY
        # starts a fresh compare below.
        self._stop_meter_compare(ch)
        if mode == "OFF":
            _vs.stop_sweep_log(ch)
            cmd = f"{ch},OFF"
            _vs.send_off(ch)
            _vs_log(f"[Apply Ch {ch}] {cmd}")
            self._set_active_mode(ch, "OFF")
        elif mode == "STEADY":
            try:
                voltage_raw = float(self._voltage_inputs[ch].get_value() or "0.0")
                voltage = _clamp_voltage(voltage_raw)
                if voltage != voltage_raw:
                    _vs_log(f"[Apply Ch {ch}] Clamped V {voltage_raw} → {voltage} (range {_V_MIN} to {_V_MAX})")
                    self._voltage_inputs[ch].set_value(str(voltage))
                duration = float(self._duration_inputs[ch].get_value() or "10")
                if self._timer_checkboxes[ch].get_value():
                    time_unit = self._time_unit_dds[ch].get_value()
                else:
                    time_unit = "Inf"
                _vs.stop_sweep_log(ch)
                cmd = f"{ch},STEADY,{voltage},{duration},{time_unit}"
                _vs.send_steady(ch, voltage, duration, time_unit)
                _vs_log(f"[Apply Ch {ch}] {cmd}")
                self._applied_voltages[ch] = voltage
                self._set_active_mode(ch, "STEADY")

                # --- meter compare -----------------------------------------
                cb = self._meter_compare_cbs[ch]
                if cb is not None and cb.get_value():
                    dur_s = self._timer_duration_seconds(ch)
                    if dur_s is None or dur_s <= 0:
                        _vs_log(
                            f"[Apply Ch {ch}] meter compare requires Timer on "
                            f"with a finite duration; skipping."
                        )
                    else:
                        try:
                            poll_ms = int(float(self._meter_poll_inputs[ch].get_value() or "500"))
                        except ValueError:
                            poll_ms = 500
                        if poll_ms < 10:
                            poll_ms = 10
                        self._start_meter_compare(ch, dur_s, poll_ms)
            except ValueError as e:
                _vs_log(f"[Apply Ch {ch}] Invalid STEADY params: {e}")
        elif mode == "SWEEP":
            try:
                range_raw = float(self._range_inputs[ch].get_value() or "1.0")
                range_v = _clamp_sweep_range(range_raw)
                if range_v != range_raw:
                    _vs_log(f"[Apply Ch {ch}] Clamped Range {range_raw} → {range_v} (max ±{_SWEEP_RANGE_MAX} V)")
                    self._range_inputs[ch].set_value(str(range_v))
                step_size = float(self._step_inputs[ch].get_value() or "10")
                # Clear buffered data so this sweep's plot doesn't inherit prior points
                _vs.clear_channel_data(ch)
                self._sweep_active[ch] = False
                path = _vs.start_sweep_log(ch)
                cmd = f"{ch},SWEEP,{range_v},{step_size}"
                _vs.send_sweep(ch, range_v, step_size)
                _vs_log(f"[Apply Ch {ch}] {cmd}")
                if path:
                    _vs_log(f"[Apply Ch {ch}] Sweep CSV → {path}")
                self._set_active_mode(ch, "SWEEP")
            except ValueError as e:
                _vs_log(f"[Apply Ch {ch}] Invalid SWEEP params: {e}")

    def _on_stop(self, ch: int):
        _vs.send_off(ch)
        _vs_log(f"[Stop Ch {ch}] sent {ch},OFF")
        self._stop_meter_compare(ch)
        self._set_active_mode(ch, "OFF")

    def _on_set_odr(self, ch: int):
        if not _vs.is_connected:
            _vs_log(f"[ODR Ch {ch}] Not connected")
            return
        dd = self._odr_dds[ch]
        if dd is None:
            return
        try:
            rate = float((dd.get_value() or _ODR_DEFAULT).strip())
        except ValueError:
            _vs_log(f"[ODR Ch {ch}] Invalid rate: {dd.get_value()!r}")
            return
        _vs.send_odr(ch, rate)
        _vs_log(f"[ODR Ch {ch}] sent {ch},ODR,{rate}")

    def _on_recalibrate(self, ch: int):
        if not _vs.is_connected:
            _vs_log(f"[Recalibrate Ch {ch}] Not connected")
            return
        try:
            r_1k = float((self._cal_r1k_inputs[ch].get_value() or "").strip())
            r_gain = float((self._cal_rgain_inputs[ch].get_value() or "").strip())
            vref = float((self._cal_vref_inputs[ch].get_value() or "").strip())
        except (ValueError, AttributeError) as e:
            _vs_log(
                f"[Recalibrate Ch {ch}] Invalid input — all three fields "
                f"(R_1k, R_gain, DAC Vref) must be numeric: {e}"
            )
            return
        _vs.send_calibration(ch, r_1k, r_gain, vref)
        _vs_log(
            f"[Recalibrate Ch {ch}] sent {ch},CALIBRATION,"
            f"{r_1k},{r_gain},{vref}"
        )

    def _set_active_mode(self, ch: int, mode: str):
        """Update the tracked mode for a channel and refresh dot + readout visibility."""
        self._active_modes[ch] = mode
        dot = self._status_dots[ch]
        if dot is not None:
            dot.style["color"] = _DOT_OFF_COLOR if mode == "OFF" else _DOT_ON_COLOR
        readout = self._steady_readouts[ch]
        if readout is not None:
            if mode == "STEADY":
                readout.style["display"] = "block"
                readout.set_text(
                    f"Set: {self._applied_voltages[ch]:.3f} V   |   Measured: — µA"
                )
            else:
                readout.style["display"] = "none"

    # ------------------------------------------------------------------
    # Connection callbacks
    # ------------------------------------------------------------------

    def _on_refresh(self):
        self._populate_ports()

    def _on_refresh_log(self):
        global _log_seq
        del _log_lines[:]
        _log_seq += 1
        if self._log_text is not None:
            self._log_text.set_text("")
            self._last_log_seq = _log_seq

    def _on_connect_toggle(self):
        if _vs.is_connected:
            threading.Thread(target=self._graceful_disconnect, daemon=True).start()
        else:
            port = self._port_dd.get_value().split("  –  ")[0].strip()
            baud = int(self._baud_dd.get_value() or 115200)
            if not port or port.startswith("--") or port == "No ports found":
                print("[VoltageSource] No port selected")
                return
            threading.Thread(
                target=_vs.connect,
                args=(port, baud),
                daemon=True,
            ).start()

    def _graceful_disconnect(self):
        """Send OFF to every channel and stop background workers *before*
        closing the serial link, so the ESP actually goes to standby
        instead of holding the last commanded state after the port drops."""
        # Cancel any per-channel meter compare workers so they don't touch
        # the ESP or meter while we're tearing things down.
        for ch in range(4):
            self._stop_meter_compare(ch)
        # Close any open sweep-CSV files.
        for ch in range(4):
            try:
                _vs.stop_sweep_log(ch)
            except Exception:
                pass
        _vs_log("[Disconnect] Sending OFF to all channels")
        for ch in range(4):
            try:
                _vs.send_off(ch)
            except Exception as e:
                _vs_log(f"[Disconnect] send_off Ch {ch} failed: {e}")
        # Give the firmware a moment to process the four OFFs before we
        # yank the port out from under it.
        time.sleep(0.2)
        _vs.disconnect()

    # ------------------------------------------------------------------
    # meter callbacks
    # ------------------------------------------------------------------

    def _on_meter_refresh_ports(self):
        self._populate_meter_ports()

    def _on_meter_connect_toggle(self):
        global _meter
        # Disconnect path
        if _meter_is_connected():
            def _do_disconnect():
                global _meter
                # Stop any running compare threads first so they don't touch
                # the meter while it's being torn down.
                for ch in range(4):
                    self._stop_meter_compare(ch)
                try:
                    _meter.disconnect()
                except Exception as e:
                    _vs_log(f"[meter] Disconnect error: {e}")
                _meter = None
                _vs_log("[meter] Disconnected")
            threading.Thread(target=_do_disconnect, daemon=True).start()
            return

        # Connect path
        if self._meter_connecting:
            return
        port_raw = self._meter_port_dd.get_value() or ""
        port = port_raw.split("  –  ")[0].strip()
        if not port or port.startswith("--") or port == "No ports found":
            _vs_log("[meter] No port selected")
            return

        self._meter_connecting = True
        _vs_log(f"[meter] Connecting to {port}")

        def _do_connect():
            global _meter
            try:
                mgr = ScpiMeterController()
                mgr.log_callback = _vs_log
                if mgr.connect(port):
                    _meter = mgr
                    _vs_log(f"[meter] Connected: {port}")
                else:
                    _vs_log("[meter] Connection failed")
            except Exception as e:
                _vs_log(f"[meter] Connection error: {e}")
            finally:
                self._meter_connecting = False

        threading.Thread(target=_do_connect, daemon=True).start()

    def _on_meter_compare_toggle(self, ch: int, value):
        """Enforce one-at-a-time meter compare: checking one channel unchecks the
        other three."""
        enabled = bool(value)
        if not enabled:
            return
        for other in range(4):
            if other == ch:
                continue
            cb = self._meter_compare_cbs[other]
            if cb is None:
                continue
            try:
                if cb.get_value():
                    cb.set_value(False)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # meter compare worker
    # ------------------------------------------------------------------

    def _start_meter_compare(self, ch: int, duration_s: float, poll_ms: int):
        """Kick off a background thread that samples the meter every poll_ms
        for duration_s seconds. Called from _on_apply for STEADY mode."""
        if not _meter_is_connected():
            _vs_log(f"[Ch {ch}] meter compare skipped: Meter not connected")
            return
        # Stop any prior compare for this channel first.
        self._stop_meter_compare(ch)

        stop_event = threading.Event()
        self._meter_compare_stops[ch] = stop_event
        self._meter_compare_data[ch] = []

        t = threading.Thread(
            target=self._meter_compare_worker,
            args=(ch, duration_s, poll_ms, stop_event),
            daemon=True,
        )
        self._meter_compare_threads[ch] = t
        t.start()

    def _stop_meter_compare(self, ch: int):
        ev = self._meter_compare_stops[ch]
        if ev is not None:
            ev.set()
        # Don't join — the worker exits on its own; we don't want to block
        # the GUI thread if the meter read is currently blocked.
        self._meter_compare_stops[ch] = None
        self._meter_compare_threads[ch] = None

    def _meter_compare_worker(self, ch: int, duration_s: float, poll_ms: int,
                            stop_event: threading.Event):
        global _meter
        poll_s = max(0.01, poll_ms / 1000.0)
        t0 = time.time()
        end_ts = t0 + max(0.0, duration_s)

        os.makedirs(_METER_OUTPUT_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(_METER_OUTPUT_DIR, f"vs_ch{ch}_meter_compare_{ts}.csv")
        self._meter_compare_paths[ch] = path

        try:
            f = open(path, "w", newline="", encoding="utf-8")
            w = csv.writer(f)
            # Only current is used for comparison; drop meter voltage. PCB reading
            # comes from the latest firmware ADC sample buffered by _vs.
            w.writerow(["elapsed_s", "pcb_current_uA", "meter_current_uA"])
        except Exception as e:
            _vs_log(f"[Ch {ch}] meter compare CSV open error: {e}")
            return

        _vs_log(f"[Ch {ch}] meter compare → {path} (poll {poll_ms} ms, dur {duration_s:.1f} s)")

        try:
            while not stop_event.is_set() and time.time() < end_ts:
                if not _meter_is_connected():
                    _vs_log(f"[Ch {ch}] meter compare: disconnected mid-run, stopping")
                    break
                try:
                    meter_i_A = _meter.measure_current()
                except Exception as e:
                    _vs_log(f"[Ch {ch}] meter read error: {e}")
                    # Back off briefly on read errors so we don't hammer the meter
                    if stop_event.wait(poll_s):
                        break
                    continue

                # Snapshot the latest PCB ADC reading. Firmware sends µA.
                pcb_buf = _vs.get_channel_data(ch)
                pcb_i_uA = pcb_buf[-1].current if pcb_buf else 0.0
                meter_i_uA = meter_i_A * 1e6

                elapsed = time.time() - t0
                self._meter_compare_data[ch].append((elapsed, pcb_i_uA, meter_i_uA))
                try:
                    w.writerow([f"{elapsed:.3f}", f"{pcb_i_uA:.6f}", f"{meter_i_uA:.6f}"])
                    f.flush()
                except Exception:
                    pass
                _vs_log(
                    f"[Ch {ch}] t={elapsed:6.2f}s  "
                    f"PCB={pcb_i_uA:11.5f} µA  meter={meter_i_uA:9.3f} µA"
                )

                # Wait poll_s, but wake early on stop.
                if stop_event.wait(poll_s):
                    break
        finally:
            try:
                f.close()
            except Exception:
                pass
        _vs_log(f"[Ch {ch}] meter compare complete ({len(self._meter_compare_data[ch])} samples)")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _populate_ports(self):
        """Rebuild the port dropdown and auto-select the best USB match."""
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
                target = f"{device}  –  {description}"
                try:
                    self._port_dd.select_by_value(target)
                    _vs_log(f"[Port] Auto-selected {device}")
                except Exception:
                    pass
                break

    def _populate_meter_ports(self):
        """Rebuild the meter port dropdown with all VISA USB resources."""
        try:
            resources = ScpiMeterController.list_devices()
        except Exception as e:
            _vs_log(f"[meter] VISA list_devices error: {e}")
            resources = []

        self._meter_port_dd.empty()
        if not resources:
            self._meter_port_dd.append("No ports found")
            return
        for r in resources:
            self._meter_port_dd.append(f"{r}  –  USB")
        # Auto-select the first resource — usually the only one.
        try:
            self._meter_port_dd.select_by_value(f"{resources[0]}  –  USB")
            _vs_log(f"[meter] Auto-selected {resources[0]}")
        except Exception:
            pass


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def _print_server_urls(port: int):
    import socket
    urls = [f"http://127.0.0.1:{port}/", f"http://localhost:{port}/"]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
        if lan_ip and lan_ip not in ("127.0.0.1",):
            urls.append(f"http://{lan_ip}:{port}/")
    except Exception:
        pass
    bar = "=" * 60
    print(bar, flush=True)
    print("Voltage Source GUI available at:", flush=True)
    for u in urls:
        print(f"  → {u}", flush=True)
    print(bar, flush=True)


def run_remi():
    cfg = get_window_config("voltage_source")
    port = cfg.get("port", 8006)
    print(f"Remi start() called on port {port}", flush=True)
    _print_server_urls(port)
    start(
        VoltageSourceApp,
        address="0.0.0.0",
        port=port,
        start_browser=False,
        multiple_instance=True,
        enable_file_cache=False,
    )
    print("Remi start() returned", flush=True)


def _shutdown_channels():
    """Send OFF to all 4 channels and disconnect — best-effort cleanup."""
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

    def _on_signal(signum, _frame):
        # sys.exit triggers normal teardown -> atexit fires _shutdown_channels
        sys.exit(0)

    for sig_name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue  # SIGHUP not on Windows, SIGBREAK not on POSIX
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            # Signals can only be set in the main thread
            pass


if __name__ == "__main__":
    try:
        print("Starting...", flush=True)
        _install_shutdown_hooks()
        cfg = get_window_config("voltage_source")
        port = cfg.get("port", 8006)
        print(f"Port: {port}", flush=True)

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
            print("Running Remi server...", flush=True)
            run_remi()
    except Exception:
        import traceback
        traceback.print_exc()
