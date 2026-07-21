"""worker.py — ZMQ worker thread for RE Manager communication."""

import json
import os
import queue as _queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from PyQt6.QtCore import QObject, pyqtSignal
from bluesky_queueserver_api.zmq import REManagerAPI
from .config import ZMQ_CONTROL, ZMQ_INFO


# ── Direct ZMQ console subscriber ─────────────────────────────────────────────

class _DirectConsoleMonitor:
    """
    Subscribes to the RE Manager's ZMQ info PUB socket and extracts console
    output messages.  This bypasses bluesky_queueserver_api's own
    console_monitor to avoid version-specific format issues.
    """

    def __init__(self):
        self._q      = _queue.Queue()
        self._thread = None
        self._active = False

    def start(self, info_addr: str) -> str:
        self.stop()
        self._active = True
        self._thread = threading.Thread(
            target=self._run, args=(info_addr,), daemon=True
        )
        self._thread.start()
        return f"Console monitor enabled — subscribed to {info_addr}"

    def stop(self):
        self._active = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None
        self._flush()

    def _flush(self):
        try:
            while True:
                self._q.get_nowait()
        except _queue.Empty:
            pass

    def drain(self) -> list:
        msgs = []
        try:
            while True:
                msgs.append(self._q.get_nowait())
        except _queue.Empty:
            pass
        return msgs

    def _run(self, info_addr: str):
        try:
            import zmq
        except ImportError:
            self._q.put("[Console] pyzmq not available.\n")
            return

        ctx = zmq.Context()
        try:
            sock = ctx.socket(zmq.SUB)
            sock.setsockopt(zmq.RCVTIMEO, 500)   # unblock every 500 ms to check _active
            sock.setsockopt(zmq.SUBSCRIBE, b"")   # receive all topics
            sock.connect(info_addr)
        except Exception as e:
            self._q.put(f"[Console] Could not connect to {info_addr}: {e}\n")
            ctx.term()
            return

        while self._active:
            try:
                parts = sock.recv_multipart()
            except zmq.Again:
                continue          # normal timeout
            except Exception:
                break

            text = self._extract(parts)
            if text:
                self._q.put(text)

        try:
            sock.close()
            ctx.term()
        except Exception:
            pass

    @staticmethod
    def _extract(parts: list) -> str:
        """
        Parse one ZMQ message (may be 1 or 2 frames).

        bluesky-queueserver typically sends:
          • single frame  → JSON dict  {"type": "console_output", "msg": "…"}
          • two frames    → [topic, JSON dict]

        When the environment is open the PUB socket also broadcasts manager-
        status dicts; those have no "type" key and are silently ignored.
        """
        for frame in parts:
            if not frame:
                continue
            try:
                obj = json.loads(frame)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            msg_type = obj.get("type", "")
            if msg_type == "console_output" or "console" in msg_type.lower():
                return obj.get("msg", "") or obj.get("text", "") or ""
        return ""

_USER_SCRIPTS_DIR = Path.home() / ".easy_bluesky" / "scripts"
_PKG_SCRIPTS_DIR  = Path(__file__).parent / "scripts"

_BUNDLED_FILES = [
    "existing_plans_and_devices.yaml",
    "user_group_permissions.yaml",
    "re_startup_mongo.py",
    "re_startup_sim.py",
    "devices.py",
    "start_re_managers.sh",
    "stop_re_managers.sh",
    "re-manager-real.service",
    "re-manager-sim.service",
]

_EXECUTABLE_SCRIPTS = {"start_re_managers.sh", "stop_re_managers.sh"}

def _get_scripts_dir() -> Path:
    """
    Return the user scripts directory (~/.easy_bluesky/scripts/), creating it
    and copying bundled defaults the first time it is needed.
    """
    _USER_SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    for fname in _BUNDLED_FILES:
        dest = _USER_SCRIPTS_DIR / fname
        if not dest.exists():
            src = _PKG_SCRIPTS_DIR / fname
            if src.exists():
                shutil.copy2(src, dest)
                if fname in _EXECUTABLE_SCRIPTS:
                    os.chmod(dest, 0o755)
    return _USER_SCRIPTS_DIR

class ZMQWorker(QObject):
    status_updated  = pyqtSignal(dict)
    queue_updated   = pyqtSignal(list)
    history_updated = pyqtSignal(list)
    plans_updated   = pyqtSignal(dict)
    devices_updated = pyqtSignal(dict)
    error_occurred  = pyqtSignal(str)
    connected       = pyqtSignal()
    disconnected    = pyqtSignal()
    re_manager_started = pyqtSignal(int)   # pid
    console_updated = pyqtSignal(str)      # new console text since last poll

    def __init__(self):
        super().__init__()
        self.rm              = None
        self._active         = True
        self._poll_interval  = 1.0
        self._re_proc        = None
        self._is_connecting  = False   # blocks poll while connect() runs
        self._console_mon    = _DirectConsoleMonitor()

    def connect(self, zmq_control=None, zmq_info=None):
        self._is_connecting = True
        try:
            self.rm = REManagerAPI(
                zmq_control_addr=zmq_control or ZMQ_CONTROL,
                zmq_info_addr=zmq_info or ZMQ_INFO,
            )
            status = self.rm.status()
            self.connected.emit()
            self.status_updated.emit(status)
            self._load_plans_devices()
            info_addr = zmq_info or ZMQ_INFO
            msg = self._console_mon.start(info_addr)
            self.console_updated.emit(f"[EasyBluesky] {msg}\n")
            return True
        except Exception as e:
            self.rm = None
            self.error_occurred.emit(f"Connection failed: {e}")
            return False
        finally:
            self._is_connecting = False

    @property
    def sim_mode(self) -> bool:
        """Kept for backward compatibility — always returns False in profile mode."""
        return False

    def start_re_manager(self, profile: dict,
                         ctrl_port: int = None, info_port: int = None):
        """
        Launch start-re-manager locally for the given profile.

        Ports come from the profile dict unless overridden by ctrl_port/info_port.
        Sets EASY_BLUESKY_DEVICES_FILE so re_startup_mongo.py loads the right devices.
        """
        exe = shutil.which("start-re-manager")
        if not exe:
            self.error_occurred.emit("start-re-manager not found — install bluesky-queueserver")
            return False

        if self._re_proc and self._re_proc.poll() is None:
            self.error_occurred.emit("RE Manager is already running")
            return False

        p_ctrl = ctrl_port if ctrl_port is not None else profile.get("control_port", 60615)
        p_info = info_port if info_port is not None else profile.get("info_port", 60625)
        devices_file = profile.get("devices_file", "devices.py")

        scripts_dir = _get_scripts_dir()
        startup_script = scripts_dir / "re_startup_mongo.py"
        existing_pd    = scripts_dir / "existing_plans_and_devices.yaml"
        permissions    = scripts_dir / "user_group_permissions.yaml"

        cmd = [exe,
               "--zmq-control-addr", f"tcp://*:{p_ctrl}",
               "--zmq-info-addr",    f"tcp://*:{p_info}",
               "--zmq-publish-console", "ON",
               "--existing-plans-devices", str(existing_pd),
               "--user-group-permissions", str(permissions)]
        if startup_script.exists():
            cmd += ["--startup-script", str(startup_script)]

        # Pass the devices file via environment variable
        env = dict(os.environ)
        env["EASY_BLUESKY_DEVICES_FILE"] = devices_file

        try:
            self._re_proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, env=env)
            self.re_manager_started.emit(self._re_proc.pid)
            return True
        except Exception as e:
            self.error_occurred.emit(f"Failed to start RE Manager: {e}")
            return False

    def stop_re_manager(self):
        """Terminate the RE Manager process started by this app."""
        if self._re_proc and self._re_proc.poll() is None:
            self._re_proc.terminate()
            self._re_proc = None

    def _load_plans_devices(self):
        try:
            plans   = self.rm.plans_allowed()
            devices = self.rm.devices_allowed()
            self.plans_updated.emit(plans.get("plans_allowed", {}))
            self.devices_updated.emit(devices.get("devices_allowed", {}))
        except Exception as e:
            self.error_occurred.emit(f"Failed to load plans/devices: {e}")

    def reload_plans_devices(self):
        """Re-fetch allowed plans and devices from the RE Manager."""
        if self.rm:
            self._load_plans_devices()

    def diagnose_console(self, info_addr: str, duration: float = 6.0) -> str:
        """
        Subscribe directly to info_addr for *duration* seconds and report
        every message type received.  Returns a multi-line diagnostic string.
        Runs synchronously — call from a background thread.
        """
        try:
            import zmq
        except ImportError:
            return "  pyzmq not installed — cannot test ZMQ socket.\n"

        lines = [f"  Subscribing to {info_addr} for {duration:.0f} s…\n"]
        ctx = zmq.Context()
        try:
            sock = ctx.socket(zmq.SUB)
            sock.setsockopt(zmq.RCVTIMEO, 500)
            sock.setsockopt(zmq.SUBSCRIBE, b"")
            sock.connect(info_addr)
        except Exception as e:
            ctx.term()
            return f"  Could not connect socket: {e}\n"

        deadline = time.monotonic() + duration
        total, console_msgs, types_seen = 0, 0, set()
        while time.monotonic() < deadline:
            try:
                parts = sock.recv_multipart()
                total += 1
                for frame in parts:
                    try:
                        obj = json.loads(frame)
                        if isinstance(obj, dict):
                            t = obj.get("type", "(no type)")
                            types_seen.add(t)
                            if t == "console_output":
                                console_msgs += 1
                    except Exception:
                        pass
            except zmq.Again:
                pass

        try:
            sock.close()
            ctx.term()
        except Exception:
            pass

        if total == 0:
            lines.append(
                "  ✗ No ZMQ frames received.\n"
                "    The info port may be bound but not routing to this host,\n"
                "    or the RE Manager is not publishing on that address.\n"
            )
        else:
            lines.append(f"  ✓ Received {total} frames in {duration:.0f} s.\n")
            lines.append(f"    Message types seen: {sorted(types_seen)}\n")
            if console_msgs:
                lines.append(f"    console_output messages: {console_msgs}\n")
            else:
                lines.append(
                    "    ✗ No console_output messages seen.\n"
                    "    RE Manager may not have been started with --zmq-publish-console ON,\n"
                    "    or the environment was not open during the test.\n"
                )
        return "".join(lines)


    def poll(self):
        while self._active:
            if self.rm and not self._is_connecting:
                try:
                    status  = self.rm.status()
                    self.status_updated.emit(status)
                    queue   = self.rm.queue_get()
                    history = self.rm.history_get()
                    self.queue_updated.emit(queue.get("items", []))
                    self.history_updated.emit(history.get("items", []))
                    # Drain console messages collected by the ZMQ subscriber thread
                    msgs = self._console_mon.drain()
                    if msgs:
                        self.console_updated.emit("".join(msgs))
                except Exception:
                    if not self._is_connecting:
                        self.rm = None
                        self.disconnected.emit()
            time.sleep(self._poll_interval)

    def disconnect(self):
        """Drop the ZMQ connection immediately without stopping the poll loop."""
        self._console_mon.stop()
        self.rm = None
        self.disconnected.emit()

    def stop(self):
        self._active = False

    # ── Queue operations ───────────────────────────────────────────────────────
    def execute_item(self, item):
        """Execute an item immediately, bypassing queue waiting."""
        try:
            r = self.rm.item_execute(item=item)
            if r.get("success"):
                return True, "Executing immediately"
            return False, r.get("msg", "Unknown error")
        except Exception as e:
            return False, str(e)

    def add_item(self, item):
        try:
            r = self.rm.item_add(item=item)
            if r.get("success"):
                return True, "Plan added to queue"
            return False, r.get("msg", "Unknown error")
        except Exception as e:
            return False, str(e)

    def update_item(self, item):
        try:
            r = self.rm.item_update(item=item, replace=True)
            if r.get("success"):
                return True, "Plan updated"
            return False, r.get("msg", "Unknown error")
        except Exception as e:
            return False, str(e)

    def remove_item(self, uid):
        try:
            r = self.rm.item_remove(uid=uid)
            if r.get("success"):
                return True, "Plan removed"
            return False, r.get("msg", "Unknown error")
        except Exception as e:
            return False, str(e)

    def move_item(self, uid, pos_dest):
        try:
            r = self.rm.item_move(uid=uid, pos_dest=pos_dest)
            return r.get("success", False), r.get("msg", "")
        except Exception as e:
            return False, str(e)

    def clear_queue(self):
        try:
            r = self.rm.queue_clear()
            return r.get("success", False), r.get("msg", "")
        except Exception as e:
            return False, str(e)

    def clear_history(self):
        try:
            r = self.rm.history_clear()
            return r.get("success", False), r.get("msg", "")
        except Exception as e:
            return False, str(e)

    # ── RE operations ──────────────────────────────────────────────────────────
    def queue_start(self):
        try:
            r = self.rm.queue_start()
            return r.get("success", False), r.get("msg", "")
        except Exception as e:
            return False, str(e)

    def queue_stop(self):
        try:
            r = self.rm.queue_stop()
            return r.get("success", False), r.get("msg", "")
        except Exception as e:
            return False, str(e)

    def re_pause(self, option="deferred"):
        try:
            r = self.rm.re_pause(option=option)
            return r.get("success", False), r.get("msg", "")
        except Exception as e:
            return False, str(e)

    def re_resume(self):
        try:
            r = self.rm.re_resume()
            return r.get("success", False), r.get("msg", "")
        except Exception as e:
            return False, str(e)

    def re_abort(self):
        try:
            r = self.rm.re_abort()
            return r.get("success", False), r.get("msg", "")
        except Exception as e:
            return False, str(e)

    def re_stop(self):
        try:
            r = self.rm.re_stop()
            return r.get("success", False), r.get("msg", "")
        except Exception as e:
            return False, str(e)

    def open_environment(self):
        try:
            r = self.rm.environment_open()
            return r.get("success", False), r.get("msg", "")
        except Exception as e:
            return False, str(e)

    def close_environment(self):
        try:
            r = self.rm.environment_close()
            return r.get("success", False), r.get("msg", "")
        except Exception as e:
            return False, str(e)

    def upload_script(self, script):
        try:
            r = self.rm.script_upload(script=script)
            return r.get("success", False), r.get("msg", "")
        except Exception as e:
            return False, str(e)
