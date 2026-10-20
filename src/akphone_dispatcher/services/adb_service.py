"""Dynamic ADB service with direct Socket Protocol (TCP 5037) and device watchdog."""

from datetime import datetime
import os
from pathlib import Path
import socket
import subprocess
import time
from typing import Dict, List, Optional, Set, Tuple

from akphone_dispatcher.core.models import DeviceInfo
from akphone_dispatcher.utils.logger import logger

# Printed after `ls` with its exit code: no mark = the command never finished on the phone.
_LS_END_MARK = "__AKLS_RC="


class AdbSocketClient:
    """High-performance direct socket client for ADB server (TCP port 5037).

    Bypasses subprocess spawning overhead for concurrent phone farms.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 5037, adb_path: str = ""):
        self.host = host
        self.port = port
        self.adb_path = adb_path

    def _ensure_server(self):
        """Ensure adb server daemon is running if socket connection fails."""
        if self.adb_path and os.path.isfile(self.adb_path):
            try:
                subprocess.run(
                    [self.adb_path, "start-server"],
                    capture_output=True,
                    timeout=6,
                )
            except Exception:
                pass

    def _connect(self, timeout: float = 3.0) -> Optional[socket.socket]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((self.host, self.port))
            return s
        except (ConnectionRefusedError, socket.error):
            self._ensure_server()
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(timeout)
                s.connect((self.host, self.port))
                return s
            except Exception:
                return None

    @staticmethod
    def _read_exact(sock: socket.socket, num_bytes: int) -> bytes:
        buf = bytearray()
        while len(buf) < num_bytes:
            chunk = sock.recv(num_bytes - len(buf))
            if not chunk:
                break
            buf.extend(chunk)
        return bytes(buf)

    def _send_cmd(self, sock: socket.socket, cmd: str):
        payload = f"{len(cmd):04x}{cmd}".encode("utf-8")
        sock.sendall(payload)

    def _read_status(self, sock: socket.socket) -> Tuple[bool, str]:
        status = self._read_exact(sock, 4)
        if status == b"OKAY":
            return True, ""
        elif status == b"FAIL":
            hex_len = self._read_exact(sock, 4)
            try:
                err_len = int(hex_len, 16)
                err_msg = self._read_exact(sock, err_len).decode("utf-8", errors="replace")
            except Exception:
                err_msg = "Unknown error"
            return False, err_msg
        return False, f"Unexpected response: {status!r}"

    def get_devices(self) -> Optional[List[DeviceInfo]]:
        """Query connected devices list via host:devices over socket."""
        sock = self._connect(timeout=2.5)
        if not sock:
            return None
        try:
            self._send_cmd(sock, "host:devices")
            ok, err = self._read_status(sock)
            if not ok:
                return None
            hex_len = self._read_exact(sock, 4)
            length = int(hex_len, 16)
            data = self._read_exact(sock, length).decode("utf-8", errors="replace")
            devices: List[DeviceInfo] = []
            for line in data.strip().splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and not line.startswith("*"):
                    serial, state = parts[0], parts[1]
                    devices.append(DeviceInfo(serial=serial, state=state))
            return devices
        except Exception:
            return None
        finally:
            sock.close()

    def get_device_state(self, serial: str) -> Optional[str]:
        """Check status of a specific device (device, offline, or None if disconnected)."""
        devs = self.get_devices()
        if devs is None:
            return None
        for d in devs:
            if d.serial == serial:
                return d.state
        return None

    def shell(self, serial: str, command: str, timeout: float = 8.0) -> Optional[str]:
        """Execute shell command on target serial via direct socket streaming."""
        sock = self._connect(timeout=timeout)
        if not sock:
            return None
        try:
            sock.settimeout(timeout)
            # 1. Transport to device
            self._send_cmd(sock, f"host:transport:{serial}")
            ok, _ = self._read_status(sock)
            if not ok:
                return None

            # 2. Issue shell command
            self._send_cmd(sock, f"shell:{command}")
            ok, _ = self._read_status(sock)
            if not ok:
                return None

            # 3. Stream output until EOF
            chunks = []
            while True:
                try:
                    data = sock.recv(4096)
                    if not data:
                        break
                    chunks.append(data)
                except socket.timeout:
                    break
            return b"".join(chunks).decode("utf-8", errors="replace").strip()
        except Exception:
            return None
        finally:
            sock.close()

    def exec_binary(self, serial: str, command: str, timeout: float = 10.0) -> Optional[bytes]:
        """Execute binary stream command (e.g. screencap -p) without PTY translation."""
        sock = self._connect(timeout=timeout)
        if not sock:
            return None
        try:
            sock.settimeout(timeout)
            self._send_cmd(sock, f"host:transport:{serial}")
            ok, _ = self._read_status(sock)
            if not ok:
                return None

            self._send_cmd(sock, f"exec:{command}")
            ok, _ = self._read_status(sock)
            if not ok:
                return None

            chunks = []
            while True:
                try:
                    data = sock.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
                except socket.timeout:
                    break
            return b"".join(chunks)
        except Exception:
            return None
        finally:
            sock.close()


class AdbService:
    """Manages communications with Android devices using Socket Protocol with subprocess fallback."""

    def __init__(self, adb_path: str = ""):
        self.adb_path = adb_path
        self.socket_client = AdbSocketClient(adb_path=adb_path)

    def is_available(self) -> bool:
        return bool(self.adb_path and os.path.isfile(self.adb_path))

    def get_device_state(self, serial: str) -> str:
        """Return device state: 'device', 'offline', or 'disconnected'."""
        state = self.socket_client.get_device_state(serial)
        if state is not None:
            return state

        # Fallback
        devices = self.get_connected_devices()
        for d in devices:
            if d.serial == serial:
                return d.state
        return "disconnected"

    def is_device_ready(self, serial: str) -> bool:
        """Fast check if the device is connected and in 'device' operational state."""
        return self.get_device_state(serial) == "device"

    def get_connected_devices(self) -> List[DeviceInfo]:
        """Detect and query all connected Android devices."""
        # 1. Try high-performance direct socket protocol first
        devices = self.socket_client.get_devices()

        # 2. Fallback to CLI subprocess if socket is unavailable
        if devices is None:
            if not self.is_available():
                return []
            devices = []
            try:
                res = subprocess.run(
                    [self.adb_path, "devices"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                lines = res.stdout.strip().splitlines()[1:]
                for line in lines:
                    parts = line.strip().split()
                    if len(parts) >= 2 and not line.startswith("*"):
                        serial, state = parts[0], parts[1]
                        devices.append(DeviceInfo(serial=serial, state=state))
            except Exception as e:
                logger.error(f"Lỗi khi quét thiết bị ADB qua subprocess: {e}")
                return []

        # Populate device hardware details for active devices
        for d in devices:
            if d.is_ready:
                d.model = self._get_device_prop(d.serial, "ro.product.model", "Android")
                d.brand = self._get_device_prop(d.serial, "ro.product.brand", "Generic")
                d.android_version = self._get_device_prop(
                    d.serial, "ro.build.version.release", "?"
                )
        return devices

    def _get_device_prop(self, serial: str, prop: str, default: str = "") -> str:
        val = self.socket_client.shell(serial, f"getprop {prop}", timeout=2.5)
        if val is not None and val.strip():
            return val.strip()

        # Fallback to subprocess
        if self.is_available():
            try:
                res = subprocess.run(
                    [self.adb_path, "-s", serial, "shell", f"getprop {prop}"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                res_val = res.stdout.strip()
                return res_val if res_val else default
            except Exception:
                return default
        return default

    def resolve_target_device(self, target_pref: str = "auto") -> Optional[DeviceInfo]:
        """Resolve device object based on 'auto' preference or explicit serial number."""
        devices = self.get_connected_devices()
        ready_devices = [d for d in devices if d.is_ready]
        if not ready_devices:
            return None

        if target_pref == "auto" or not target_pref:
            return ready_devices[0]

        for d in ready_devices:
            if d.serial == target_pref:
                return d

        logger.warning(
            f"Thiết bị '{target_pref}' không khả dụng, tự động chuyển sang: {ready_devices[0].serial}"
        )
        return ready_devices[0]

    def run_shell(self, serial: str, command: str, timeout: int = 8) -> str:
        """Run an arbitrary shell command on target device using socket with subprocess fallback."""
        # 1. Direct Socket execution
        out = self.socket_client.shell(serial, command, timeout=float(timeout))
        if out is not None:
            return out

        # 2. Subprocess fallback
        if not self.is_available():
            raise RuntimeError("Đường dẫn adb.exe không hợp lệ và ADB socket không khả dụng.")
        res = subprocess.run(
            [self.adb_path, "-s", serial, "shell", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return res.stdout.strip()

    def take_screenshot(self, serial: str, output_path: Path) -> bool:
        """Capture screenshot from Android screen directly to local file."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 1. Try socket exec binary first
        raw_bytes = self.socket_client.exec_binary(serial, "screencap -p", timeout=10.0)
        if raw_bytes and len(raw_bytes) > 1024 and raw_bytes.startswith(b"\x89PNG"):
            try:
                with open(output_path, "wb") as f:
                    f.write(raw_bytes)
                logger.info(f"📸 [ADB Socket] Đã chụp ảnh màn hình lỗi: {output_path}")
                return True
            except Exception as e:
                logger.error(f"Lỗi ghi ảnh chụp màn hình: {e}")

        # 2. Fallback to subprocess
        if self.is_available():
            try:
                with open(output_path, "wb") as f:
                    res = subprocess.run(
                        [self.adb_path, "-s", serial, "exec-out", "screencap", "-p"],
                        stdout=f,
                        stderr=subprocess.PIPE,
                        timeout=10,
                    )
                if res.returncode == 0 and output_path.stat().st_size > 1024:
                    logger.info(f"📸 [ADB CLI] Đã chụp ảnh màn hình lỗi: {output_path}")
                    return True
            except Exception as e:
                logger.error(f"Lỗi chụp ảnh màn hình qua ADB subprocess: {e}")
        return False

    def force_stop_facebook(self, serial: str) -> bool:
        """Force stop Facebook Katana app to release hanging state or crash popups."""
        try:
            logger.warning(f"🛡️ [Watchdog] Tắt ứng dụng Facebook trên thiết bị {serial}...")
            self.run_shell(serial, "am force-stop com.facebook.katana", timeout=5)
            self.run_shell(serial, "input keyevent KEYCODE_HOME", timeout=3)
            return True
        except Exception as e:
            logger.error(f"Lỗi khi tắt app Facebook qua ADB: {e}")
            return False

    def clean_storage(self, serial: str) -> bool:
        """Clean temporary thumbnails, DCIM cache and trash directories on phone."""
        clean_cmd = (
            "rm -rf /storage/emulated/0/DCIM/.thumbnails/* "
            "/storage/emulated/0/DCIM/Camera/.trash/* "
            "/storage/emulated/0/Android/data/com.facebook.katana/cache/* 2>/dev/null || true"
        )
        try:
            self.run_shell(serial, clean_cmd, timeout=8)
            logger.info(f"🧹 Đã dọn dẹp cache và bộ nhớ đệm điện thoại {serial}.")
            return True
        except Exception as e:
            logger.warning(f"Lỗi khi dọn dẹp bộ nhớ điện thoại: {e}")
            return False

    def init_round_flags(self, serial: str, page_ids: List[str]) -> bool:
        """Seed indicator flag files into /storage/emulated/0/Android/Done/.

        True only once the phone really holds exactly these flags: a cable pulled
        mid-command must not start a round whose flags are not there.
        """
        done_dir = "/storage/emulated/0/Android/Done"
        touch_files = " ".join([f"{done_dir}/{pid}.txt" for pid in page_ids])
        sh_cmd = (
            f"mkdir -p {done_dir} && "
            f"rm -f {done_dir}/* && "
            f"touch {touch_files} {done_dir}/round_done.txt"
        )
        try:
            self.run_shell(serial, sh_cmd, timeout=8)
        except Exception as e:
            logger.error(f"Lỗi nạp cờ Done lên điện thoại {serial}: {e}")
            return False
        expected = set(page_ids) | {"round_done"}
        present = self.list_flags(serial, done_dir)
        if present != expected:
            logger.error(
                f"Nạp cờ Done lên điện thoại {serial} chưa thành công "
                f"(đọc lại: {sorted(present) if present is not None else 'không đọc được'})."
            )
            return False
        return True

    def clear_done_flags(self, serial: str) -> bool:
        """Remove every flag in the Done directory (before feeding a new store).

        True only once the phone is read back with no flag left.
        """
        done_dir = "/storage/emulated/0/Android/Done"
        try:
            self.run_shell(serial, f"mkdir -p {done_dir} && rm -f {done_dir}/*", timeout=8)
        except Exception as e:
            logger.error(f"Lỗi xóa cờ Done trên điện thoại {serial}: {e}")
            return False
        present = self.list_flags(serial, done_dir)
        if present != set():
            logger.error(
                f"Xóa cờ Done trên điện thoại {serial} chưa thành công "
                f"(đọc lại: {sorted(present) if present is not None else 'không đọc được'})."
            )
            return False
        return True

    def list_done_flags(self, serial: str) -> Optional[Set[str]]:
        """List active flag file names currently present in the Done directory."""
        return self.list_flags(serial, "/storage/emulated/0/Android/Done")

    def touch_flag(self, serial: str, flag_dir: str, name: str) -> bool:
        """Create ``<flag_dir>/<name>.txt`` on the phone (e.g. a comment job marker)."""
        try:
            self.run_shell(serial, f"mkdir -p {flag_dir} && touch {flag_dir}/{name}.txt", timeout=6)
            return name in (self.list_flags(serial, flag_dir) or set())
        except Exception as e:
            logger.error(f"Lỗi tạo cờ {flag_dir}/{name}.txt trên điện thoại {serial}: {e}")
            return False

    def remove_flag(self, serial: str, flag_dir: str, name: str) -> bool:
        try:
            self.run_shell(serial, f"rm -f {flag_dir}/{name}.txt", timeout=5)
            return True
        except Exception as e:
            logger.error(f"Lỗi xóa cờ {flag_dir}/{name}.txt trên điện thoại {serial}: {e}")
            return False

    def list_flags(self, serial: str, flag_dir: str) -> Optional[Set[str]]:
        """List flag names (without .txt) in a phone directory.

        None whenever the answer cannot be trusted: ADB failed, the phone dropped off
        mid-command (adb then prints nothing), the output was cut, or the folder is
        missing (storage not mounted yet while the phone boots). Reading any of these
        as "no flag" would count every page as done.
        """
        try:
            output = self.run_shell(
                serial, f"ls {flag_dir} 2>&1; echo {_LS_END_MARK}$?", timeout=4
            )
        except Exception:
            return None
        if not output or _LS_END_MARK not in output:
            return None
        body, _, code = output.rpartition(_LS_END_MARK)
        if code.strip() != "0":
            return None
        flags: Set[str] = set()
        for f in body.split():
            f = f.strip()
            if f:
                flags.add(f[:-4] if f.endswith(".txt") else f)
        return flags
