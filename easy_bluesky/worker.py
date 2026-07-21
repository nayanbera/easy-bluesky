"""worker.py — ZMQ worker thread for RE Manager communication."""

import os
import shutil
import subprocess
import time
from pathlib import Path
from PyQt6.QtCore import QObject, pyqtSignal
from bluesky_queueserver_api.zmq import REManagerAPI
from .config import ZMQ_CONTROL, ZMQ_INFO

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
            # Enable console monitor permanently after connecting
            try:
                self.rm.console_monitor.enable()
            except Exception:
                pass
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
                    # Drain all pending console messages and emit as one chunk
                    try:
                        msgs = []
                        while True:
                            try:
                                msg = self.rm.console_monitor.next_msg(timeout=0)
                                msgs.append(msg.get("msg", ""))
                            except Exception:
                                break
                        if msgs:
                            self.console_updated.emit("".join(msgs))
                    except Exception:
                        pass
                except Exception:
                    if not self._is_connecting:
                        self.rm = None
                        self.disconnected.emit()
            time.sleep(self._poll_interval)

    def disconnect(self):
        """Drop the ZMQ connection immediately without stopping the poll loop."""
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
