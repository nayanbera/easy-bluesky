"""worker.py — ZMQ worker thread for RE Manager communication."""

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
]

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
        self._sim_mode       = False   # use re_startup_sim.py when True

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
        return self._sim_mode

    @sim_mode.setter
    def sim_mode(self, value: bool):
        self._sim_mode = value

    def start_re_manager(self, sim: bool | None = None,
                         ctrl_port: int = 60615, info_port: int = 60625):
        """Launch start-re-manager on the given ZMQ ports."""
        exe = shutil.which("start-re-manager")
        if not exe:
            self.error_occurred.emit("start-re-manager not found — install bluesky-queueserver")
            return False

        if self._re_proc and self._re_proc.poll() is None:
            self.error_occurred.emit("RE Manager is already running")
            return False

        use_sim = self._sim_mode if sim is None else sim
        scripts_dir    = _get_scripts_dir()
        startup_script = scripts_dir / ("re_startup_sim.py" if use_sim
                                        else "re_startup_mongo.py")
        existing_pd    = scripts_dir / "existing_plans_and_devices.yaml"
        permissions    = scripts_dir / "user_group_permissions.yaml"

        cmd = [exe,
               "--zmq-control-addr", f"tcp://*:{ctrl_port}",
               "--zmq-info-addr",    f"tcp://*:{info_port}",
               "--zmq-publish-console", "ON",
               "--existing-plans-devices", str(existing_pd),
               "--user-group-permissions", str(permissions)]
        if startup_script.exists():
            cmd += ["--startup-script", str(startup_script)]

        try:
            self._re_proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
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
