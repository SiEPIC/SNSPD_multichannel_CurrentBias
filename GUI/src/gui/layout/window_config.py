import os
import socket
from typing import Dict

WINDOW_CONFIG: Dict[str, dict] = {
    "voltage_source": {"port": 8006, "title": "Voltage Source", "width": 1400, "height": 1060},
}


def _is_port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def get_window_config(name: str) -> dict:
    """Return window config, honoring the IRIS_<NAME>_PORT env override.
    Falls back to the next free port (base+1..base+100) if the requested one is busy.
    """
    cfg = dict(WINDOW_CONFIG.get(name, {}))

    env_key = f"IRIS_{name.upper()}_PORT"
    if env_key in os.environ:
        try:
            cfg["port"] = int(os.environ[env_key])
        except ValueError:
            pass

    base = cfg.get("port")
    if base is not None and not _is_port_available(base):
        for delta in range(1, 101):
            if _is_port_available(base + delta):
                cfg["port"] = base + delta
                break

    return cfg
