import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


XDG_RUNTIME_DIR = "/run/user/0"
PULSE_RUNTIME_PATH = "/run/user/0/pulse"
PULSE_PID_PATH = Path(PULSE_RUNTIME_PATH) / "pid"


@dataclass
class BluetoothDevice:
    mac: str
    name: str
    connected: bool = False


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def pulse_env() -> dict[str, str]:
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = XDG_RUNTIME_DIR
    env["PULSE_RUNTIME_PATH"] = PULSE_RUNTIME_PATH
    env["PULSE_LATENCY_MSEC"] = "250"
    env.setdefault("HOME", "/root")
    return env


def _clean(text: str) -> str:
    return ANSI_RE.sub("", text or "").strip()


def _run(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=pulse_env(),
    )


def _bt(*args: str) -> str:
    result = _run("bluetoothctl", *args, timeout=45)
    return _clean((result.stdout or "") + (result.stderr or ""))


def _mac_key(mac: str) -> str:
    return mac.lower().replace(":", "_")


def _our_pulse_pid() -> int | None:
    try:
        pid = int(PULSE_PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return None
    if Path(f"/proc/{pid}").exists():
        return pid
    return None


def _pulse_pids() -> list[int]:
    result = subprocess.run(
        ["pgrep", "-x", "pulseaudio"],
        capture_output=True,
        text=True,
        check=False,
    )
    pids: list[int] = []
    for raw in result.stdout.split():
        try:
            pids.append(int(raw))
        except ValueError:
            continue
    return pids


def _kill_stray_pulse() -> bool:
    our = _our_pulse_pid()
    killed = False
    for pid in _pulse_pids():
        if our is not None and pid == our:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed = True
        except OSError:
            pass
    if not killed:
        return False
    time.sleep(0.5)
    our = _our_pulse_pid()
    for pid in _pulse_pids():
        if our is not None and pid == our:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    time.sleep(0.3)
    return True


def _module_rows() -> list[tuple[str, str, str]]:
    rows = []
    for line in (_run("pactl", "list", "short", "modules").stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        idx, name = parts[0], parts[1]
        args = parts[2] if len(parts) > 2 else ""
        rows.append((idx, name, args))
    return rows


def _ensure_bt_modules(force_reload: bool = False) -> None:
    rows = _module_rows()
    policy_ok = False
    discover_ok = False
    for idx, name, args in rows:
        if name == "module-bluetooth-policy":
            if not force_reload and "auto_switch=false" in args:
                policy_ok = True
            else:
                _run("pactl", "unload-module", idx)
        elif name == "module-bluetooth-discover":
            if force_reload:
                _run("pactl", "unload-module", idx)
            else:
                discover_ok = True

    rows = _module_rows()
    policy_ok = any(
        name == "module-bluetooth-policy" and "auto_switch=false" in args
        for _, name, args in rows
    )
    discover_ok = any(name == "module-bluetooth-discover" for _, name, _ in rows)

    if not policy_ok:
        _run("pactl", "load-module", "module-bluetooth-policy", "auto_switch=false")
    if not discover_ok:
        _run("pactl", "load-module", "module-bluetooth-discover")

    for idx, name, _args in _module_rows():
        if name == "module-suspend-on-idle":
            _run("pactl", "unload-module", idx)


def ensure_audio_stack() -> None:
    os.makedirs(PULSE_RUNTIME_PATH, mode=0o700, exist_ok=True)
    killed = _kill_stray_pulse()
    check = _run("pulseaudio", "--check")
    if check.returncode != 0:
        _run("pulseaudio", "--start", "--exit-idle-time=-1")
        time.sleep(1)
        killed = True
    _ensure_bt_modules(force_reload=killed)


def _device_connected(mac: str) -> bool:
    output = _bt("info", mac)
    return "Connected: yes" in output


def list_paired_devices() -> list[BluetoothDevice]:
    output = _bt("devices")
    connected_output = _bt("devices", "Connected")
    known_connected = set(re.findall(r"Device ([0-9A-F:]{17})", connected_output))

    rows: list[BluetoothDevice] = []
    seen: set[str] = set()
    for raw in output.splitlines():
        match = re.match(r"Device ([0-9A-F:]{17}) (.+)", raw.strip())
        if not match:
            continue
        mac, name = match.groups()
        info = _bt("info", mac)
        if "Paired: yes" not in info and mac not in known_connected:
            continue
        if mac in seen:
            continue
        seen.add(mac)
        rows.append(
            BluetoothDevice(
                mac=mac,
                name=name,
                connected=mac in known_connected or "Connected: yes" in info,
            )
        )
    return rows


def _find_named(kind: str, mac: str, prefixes: tuple[str, ...]) -> str | None:
    result = _run("pactl", "list", "short", kind)
    key = _mac_key(mac)
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[1]
        lowered = name.lower()
        if key in lowered and any(prefix in lowered for prefix in prefixes):
            return name
    return None


def _find_card(mac: str) -> str | None:
    return _find_named("cards", mac, ("bluez_card", "bluez"))


def _find_sink(mac: str) -> str | None:
    return _find_named("sinks", mac, ("bluez_sink", "bluez_output"))


def move_playing_streams(sink_name: str) -> str:
    result = _run("pactl", "list", "short", "sink-inputs")
    moved = 0
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        mv = _run("pactl", "move-sink-input", parts[0], sink_name)
        if mv.returncode == 0:
            moved += 1
    if moved:
        return f"Перекинул {moved} поток(а) на {sink_name}"
    return ""


def _set_a2dp_profile(mac: str) -> str:
    card = _find_card(mac)
    if not card:
        return "Bluetooth card пока не появилась в PulseAudio."
    result = _run("pactl", "set-card-profile", card, "a2dp_sink")
    err = _clean((result.stdout or "") + (result.stderr or ""))
    if result.returncode == 0:
        return f"Profile {card}: a2dp_sink"
    return f"Не удалось выставить a2dp_sink для {card}: {err or 'unknown error'}"


def _tune_a2dp_sink(mac: str, sink_name: str) -> None:
    card = _find_card(mac)
    if card:
        _run("pactl", "set-port-latency-offset", card, "portable-output", "250000")
        _run("pactl", "send-message", f"/card/{card}/bluez", "switch-codec", '"sbc"')
    _run("pactl", "set-sink-volume", sink_name, "100%")


def select_sink_for_mac(mac: str) -> str:
    sink_name = _find_sink(mac)
    if not sink_name:
        return "Bluetooth sink пока не появился в PulseAudio."

    set_result = _run("pactl", "set-default-sink", sink_name)
    if set_result.returncode != 0:
        err = _clean((set_result.stdout or "") + (set_result.stderr or ""))
        return f"Не удалось выбрать sink: {sink_name} {err}".strip()

    _tune_a2dp_sink(mac, sink_name)
    extra = move_playing_streams(sink_name)
    message = f"Default sink: {sink_name}"
    if extra:
        message = f"{message}\n{extra}"
    return message


def connect_device(mac: str) -> tuple[bool, str]:
    ensure_audio_stack()
    messages = [_bt("power", "on"), _bt("trust", mac)]

    connect_output = _bt("connect", mac)
    messages.append(connect_output)

    connected = False
    for _ in range(20):
        if _device_connected(mac):
            connected = True
            break
        time.sleep(0.5)

    sink_msg = ""
    if connected:
        profile_msg = "Bluetooth card пока не появилась в PulseAudio."
        for _ in range(20):
            profile_msg = _set_a2dp_profile(mac)
            if profile_msg.startswith("Profile "):
                break
            time.sleep(0.5)
        messages.append(profile_msg)

        sink_msg = "Bluetooth sink пока не появился в PulseAudio."
        for _ in range(40):
            sink_msg = select_sink_for_mac(mac)
            if sink_msg.startswith("Default sink:"):
                break
            time.sleep(0.5)
        messages.append(sink_msg)

    ok = sink_msg.startswith("Default sink:")
    if connected and not ok:
        cards = _clean(_run("pactl", "list", "short", "cards").stdout)
        sinks = _clean(_run("pactl", "list", "short", "sinks").stdout)
        messages.append(f"cards:\n{cards or '(пусто)'}")
        messages.append(f"sinks:\n{sinks or '(пусто)'}")
        messages.append(
            "Колонка подключена, но A2DP sink не поднялся. "
            "Проверьте, что колонка включена рядом с Pi и не занята телефоном."
        )

    message = "\n".join(part for part in messages if part)
    return ok, message


def disconnect_device(mac: str) -> tuple[bool, str]:
    output = _bt("disconnect", mac)
    ok = "Successful disconnected" in output or "successful disconnected" in output.lower()
    return ok, output
