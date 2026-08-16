"""worker.py — ZMQ worker thread for RE Manager communication."""

import json
import os
import queue as _queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot
from bluesky_queueserver_api.zmq import REManagerAPI
from .config import ZMQ_CONTROL, ZMQ_INFO, ZMQ_DOC_ADDR


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
        Parse one ZMQ message (1 or 2 frames) and return console text.

        Two formats observed in the wild:

        Format A — newer versions (single JSON frame):
            {"type": "console_output", "msg": "text…"}

        Format B — v0.0.x (two frames: topic + JSON wrapper):
            [b"QS_Console", {"time": …, "msg": {"console_output": {"text": "…"}}}]

        The topic frame (b"QS_Info" / b"QS_Console") is not valid JSON and is
        skipped.  Status frames (msg.status) have no console_output key and
        are silently ignored.
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

            # Format A: top-level "type" key
            msg_type = obj.get("type", "")
            if msg_type == "console_output" or "console" in msg_type.lower():
                return obj.get("msg", "") or obj.get("text", "") or ""

            # Format B: {"time": …, "msg": {"console_output": {"text": …}}}
            inner = obj.get("msg")
            if isinstance(inner, dict):
                co = inner.get("console_output")
                if co is not None:
                    if isinstance(co, dict):
                        return co.get("text", "") or co.get("msg", "") or ""
                    if isinstance(co, str):
                        return co
                # Nested type field variant
                inner_type = inner.get("type", "")
                if inner_type == "console_output" or "console" in inner_type.lower():
                    return inner.get("msg", "") or inner.get("text", "") or ""
        return ""

# ── SSH log-file tailer ────────────────────────────────────────────────────────

class _SSHLogTailer:
    """
    Tails a remote log file over SSH using 'tail -n 50 -f'.

    This is the reliable fallback console source for RE Manager versions that
    do not forward worker stdout to the ZMQ info socket.  The procServ log
    captures everything start-re-manager and its worker subprocess print, so
    tailing it gives full startup and plan output.

    Uses the same Queue drain interface as _DirectConsoleMonitor.
    """

    def __init__(self):
        self._q       = _queue.Queue()
        self._thread  = None
        self._active  = False
        self._channel = None
        self._client  = None

    def start(self, settings: dict, log_file: str) -> str:
        self.stop()
        self._active = True
        self._thread = threading.Thread(
            target=self._run, args=(settings, log_file), daemon=True
        )
        self._thread.start()
        return f"SSH log tail started — following {log_file}"

    def stop(self):
        self._active = False
        for obj in (self._channel, self._client):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread  = None
        self._channel = None
        self._client  = None

    def drain(self) -> list:
        msgs = []
        try:
            while True:
                msgs.append(self._q.get_nowait())
        except _queue.Empty:
            pass
        return msgs

    def _run(self, settings: dict, log_file: str):
        try:
            from .ssh_manager import _get_client
            self._client = _get_client(settings)
            transport = self._client.get_transport()
            self._channel = transport.open_session()
            self._channel.settimeout(0.5)
            # -n 50: replay the last 50 log lines immediately on connect
            self._channel.exec_command(f"tail -n 50 -f {log_file} 2>/dev/null")
            buf = ""
            while self._active:
                try:
                    data = self._channel.recv(4096)
                    if not data:
                        break   # channel closed by remote side
                    buf += data.decode("utf-8", errors="replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        self._q.put(line + "\n")
                except Exception:
                    if not self._active:
                        break
                    # Timeout (socket.timeout) is the expected path — settimeout(0.5)
                    # keeps the loop responsive.  Any other exception (connection
                    # dropped, channel closed) would otherwise spin the thread at
                    # 100 % per core.  Sleep briefly so we don't peg the CPU.
                    time.sleep(0.2)
        except Exception as e:
            if self._active:
                self._q.put(f"[Console] SSH log tail error: {e}\n")
        finally:
            for obj in (self._channel, self._client):
                try:
                    if obj is not None:
                        obj.close()
                except Exception:
                    pass


class _LocalDocWriter:
    """
    Subscribe to the bluesky ZMQ PUB document stream and write JSONL files
    locally in the active experiment's runs/ directory.

    This lets remote RE Manager scans be captured on the local machine without
    relying on the remote side having write access to any local path.
    Each run becomes <exp_dir>/runs/<uid>.jsonl in [name, doc] line format.
    """

    def __init__(self):
        self._thread   = None
        self._active   = False
        self._exp_dir  = None   # Path or None, guarded by _lock
        self._lock     = threading.Lock()
        self._open_fhs = {}     # uid → file handle (only accessed by _run thread)

    def set_exp_dir(self, path: str):
        with self._lock:
            self._exp_dir = Path(path) / "runs" if path else None

    def start(self, addr: str):
        self.stop()
        self._active = True
        self._thread = threading.Thread(
            target=self._run, args=(addr,), daemon=True
        )
        self._thread.start()

    def stop(self):
        self._active = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
        for fh in self._open_fhs.values():
            try:
                fh.flush()
                fh.close()
            except Exception:
                pass
        self._open_fhs.clear()

    def _get_exp_dir(self):
        with self._lock:
            return self._exp_dir

    def _run(self, addr: str):
        try:
            import zmq
        except ImportError:
            return

        ctx  = zmq.Context()
        sock = ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.RCVTIMEO, 500)
        sock.setsockopt(zmq.SUBSCRIBE, b"")
        try:
            sock.connect(addr)
        except Exception:
            ctx.term()
            return

        while self._active:
            try:
                raw        = sock.recv_string()
                name, doc  = json.loads(raw)
                self._handle(name, doc)
            except zmq.Again:
                continue
            except Exception:
                time.sleep(0.1)  # prevent CPU spin on persistent socket errors

        for fh in self._open_fhs.values():
            try:
                fh.flush()
                fh.close()
            except Exception:
                pass
        self._open_fhs.clear()
        sock.close()
        ctx.term()

    def _handle(self, name: str, doc: dict):
        exp_dir = self._get_exp_dir()
        if exp_dir is None:
            return

        uid = doc.get("uid") if name == "start" else doc.get("run_start")
        if not uid:
            return

        if name == "start" and uid not in self._open_fhs:
            try:
                exp_dir.mkdir(parents=True, exist_ok=True)
                self._open_fhs[uid] = open(exp_dir / f"{uid}.jsonl", "a")
            except Exception:
                return

        fh = self._open_fhs.get(uid)
        if fh is None:
            return

        try:
            fh.write(json.dumps([name, doc]) + "\n")
            fh.flush()
        except Exception:
            pass

        if name == "stop":
            try:
                fh.close()
            except Exception:
                pass
            self._open_fhs.pop(uid, None)


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

# These files are always overwritten from the package bundle so that
# updates to queueserver config (permissions, startup scripts) are
# applied without requiring the user to manually delete their copy.
_ALWAYS_OVERWRITE = {"user_group_permissions.yaml"}

_EXECUTABLE_SCRIPTS = {"start_re_managers.sh", "stop_re_managers.sh"}

def _get_scripts_dir() -> Path:
    """
    Return the user scripts directory (~/.easy_bluesky/scripts/), creating it
    and copying bundled defaults the first time it is needed.
    """
    _USER_SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    for fname in _BUNDLED_FILES:
        dest = _USER_SCRIPTS_DIR / fname
        if not dest.exists() or fname in _ALWAYS_OVERWRITE:
            src = _PKG_SCRIPTS_DIR / fname
            if src.exists():
                shutil.copy2(src, dest)
                if fname in _EXECUTABLE_SCRIPTS:
                    os.chmod(dest, 0o755)
    return _USER_SCRIPTS_DIR

class _PVNamesReader(QThread):
    """Background thread: calls get_device_pvnames() via function_execute."""
    pv_names_ready = pyqtSignal(dict)
    read_error     = pyqtSignal(str)

    def __init__(self, rm, parent=None):
        super().__init__(parent)
        self._rm = rm

    def run(self):
        try:
            from bluesky_queueserver_api import BFunc
            r = self._rm.function_execute(item=BFunc("get_device_pvnames"))
            if not r.get("success"):
                msg = r.get("msg", "function_execute failed")
                if "not found in the worker namespace" in msg or "not allowed" in msg:
                    self.read_error.emit(
                        "Restart RE Manager to enable live monitoring "
                        "(re_startup_mongo.py needs to be re-uploaded)"
                    )
                else:
                    self.read_error.emit(msg)
                return
            task_uid = r["task_uid"]
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                time.sleep(0.3)
                res = self._rm.task_result(task_uid=task_uid)
                state = res.get("status", "")
                if state == "completed":
                    result = res.get("result", {})
                    if not result.get("success", True):
                        tb = str(result.get("return_value", "unknown error"))
                        if "not found in the worker namespace" in tb:
                            self.read_error.emit(
                                "Restart RE Manager to enable live monitoring "
                                "(re_startup_mongo.py needs to be re-uploaded)"
                            )
                        else:
                            self.read_error.emit(tb[:200])
                        return
                    rv = result.get("return_value", {})
                    self.pv_names_ready.emit(rv if isinstance(rv, dict) else {})
                    return
                if state in ("failed", "aborted", "not_found"):
                    self.read_error.emit(res.get("msg", f"Task {state}"))
                    return
            self.read_error.emit("get_device_pvnames timed out")
        except Exception as e:
            self.read_error.emit(str(e))


class _DeviceStatusReader(QThread):
    """Background thread: calls read_devices_status() via function_execute and waits for result."""
    readings_ready = pyqtSignal(dict)
    read_error     = pyqtSignal(str)

    def __init__(self, rm, parent=None):
        super().__init__(parent)
        self._rm = rm

    def run(self):
        try:
            from bluesky_queueserver_api import BFunc
            r = self._rm.function_execute(item=BFunc("read_devices_status"))
            if not r.get("success"):
                self.read_error.emit(r.get("msg", "function_execute failed"))
                return
            task_uid = r["task_uid"]
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                time.sleep(0.3)
                res = self._rm.task_result(task_uid=task_uid)
                state = res.get("status", "")
                if state == "completed":
                    result = res.get("result", {})
                    if not result.get("success", True):
                        tb = result.get("return_value", "Unknown error")
                        self.read_error.emit(f"read_devices_status raised:\n{str(tb)[:300]}")
                        return
                    rv = result.get("return_value", {})
                    self.readings_ready.emit(rv if isinstance(rv, dict) else {})
                    return
                if state in ("failed", "aborted"):
                    self.read_error.emit(res.get("msg", f"Task {state}"))
                    return
                if state == "not_found":
                    self.read_error.emit(f"Task result expired (uid={task_uid})")
                    return
            self.read_error.emit("read_devices_status timed out (>15 s)")
        except Exception as e:
            self.read_error.emit(str(e))


class _SimDeviceSetter(QThread):
    """Background thread: calls set_sim_device() via function_execute and polls for completion."""
    done  = pyqtSignal(bool, str)   # success, message

    def __init__(self, rm, name: str, value: float, parent=None):
        super().__init__(parent)
        self._rm    = rm
        self._name  = name
        self._value = value

    def run(self):
        try:
            from bluesky_queueserver_api import BFunc
            r = self._rm.function_execute(
                item=BFunc("set_sim_device", name=self._name, value=self._value)
            )
            if not r.get("success"):
                self.done.emit(False, r.get("msg", "function_execute failed"))
                return
            task_uid = r["task_uid"]
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                time.sleep(0.2)
                res = self._rm.task_result(task_uid=task_uid)
                state = res.get("status", "")
                if state == "completed":
                    result = res.get("result", {})
                    if not result.get("success", True):
                        tb = str(result.get("return_value", "unknown error"))
                        self.done.emit(False, tb[:200])
                        return
                    self.done.emit(True, "")
                    return
                if state in ("failed", "aborted", "not_found"):
                    self.done.emit(False, res.get("msg", f"Task {state}"))
                    return
            self.done.emit(False, "set_sim_device timed out (>15 s)")
        except Exception as e:
            self.done.emit(False, str(e))


class _ScanLogFetcher(QThread):
    """Fetch scans_log.json from the remote RE machine via SFTP."""
    data_ready  = pyqtSignal(bytes)
    fetch_error = pyqtSignal(str)

    def __init__(self, settings: dict, remote_path: str):
        super().__init__()
        self._settings    = settings
        self._remote_path = remote_path

    def run(self):
        try:
            import io
            from .ssh_manager import _get_client
            client = _get_client(self._settings)
            sftp   = client.open_sftp()
            buf    = io.BytesIO()
            sftp.getfo(self._remote_path, buf)
            sftp.close()
            client.close()
            self.data_ready.emit(buf.getvalue())
        except Exception as e:
            self.fetch_error.emit(str(e))


class ZMQWorker(QObject):
    status_updated       = pyqtSignal(dict)
    queue_updated        = pyqtSignal(list)
    running_item_updated = pyqtSignal(dict)   # {} when idle, plan item dict when running
    history_updated      = pyqtSignal(list)
    plans_updated   = pyqtSignal(dict)
    devices_updated         = pyqtSignal(dict)
    device_readings_updated = pyqtSignal(dict)
    device_read_error       = pyqtSignal(str)
    pv_names_ready          = pyqtSignal(dict)
    pv_names_error          = pyqtSignal(str)
    sim_device_set_done     = pyqtSignal(str, bool, str)  # dev_name, success, msg
    error_occurred          = pyqtSignal(str)
    connected       = pyqtSignal()
    disconnected    = pyqtSignal()
    env_opened      = pyqtSignal()
    env_closed      = pyqtSignal()
    re_manager_started = pyqtSignal(int)   # pid
    console_updated = pyqtSignal(str)      # new console text since last poll
    scan_log_ready  = pyqtSignal(bytes)    # raw bytes of remote scans_log.json
    scan_log_error  = pyqtSignal(str)      # error message if SFTP fetch failed

    def __init__(self):
        super().__init__()
        self.rm              = None
        self._active         = True
        self._poll_interval  = 1.0
        self._re_proc        = None
        self._is_connecting  = False   # blocks poll while connect() runs
        self._console_mon    = _DirectConsoleMonitor()
        self._log_tailer     = _SSHLogTailer()
        self._doc_writer     = _LocalDocWriter()
        self._device_reader  = None   # strong ref to _DeviceStatusReader
        self._pv_names_reader = None  # strong ref to _PVNamesReader
        self._sim_device_setter = None  # strong ref to _SimDeviceSetter
        self._ssh_settings   = {}     # saved from start_log_tail for SFTP use
        self._scan_log_fetcher = None  # strong ref to _ScanLogFetcher
        # Set by main thread after script_upload; polled each cycle so
        # _load_plans_devices() always runs in the poll thread (thread-safe).
        self._reload_plans_requested = False

    @pyqtSlot(str, str)
    def connect(self, zmq_control=None, zmq_info=None, zmq_doc=None):
        self._is_connecting = True
        try:
            ctrl_addr = zmq_control or ZMQ_CONTROL
            # TCP pre-check: probe the control port before creating the ZMQ
            # API object.  ZMQ's lazy connect means the first status() call
            # blocks for timeout_recv (2 s) × several internal retries when
            # nothing is listening — totalling several minutes on Windows where
            # there is no fast ICMP unreachable.  A TCP probe gives an
            # immediate ConnectionRefusedError when the port is free (no server)
            # and times out in at most 3 s when the host is unreachable.
            try:
                _parts = ctrl_addr.replace("tcp://", "").rsplit(":", 1)
                _host, _port = (_parts[0] or "localhost"), int(_parts[1])
                import socket as _sock
                try:
                    _sock.create_connection((_host, _port), timeout=3).close()
                except ConnectionRefusedError:
                    self.rm = None
                    self.error_occurred.emit(
                        f"Nothing is listening on {_host}:{_port}.\n"
                        "Start the RE Manager first (Restart RE Manager button),\n"
                        "or check that the correct profile is selected."
                    )
                    self.disconnected.emit()
                    return False
                except OSError:
                    # Timeout or network error — let ZMQ try; it will fail with
                    # its own error message.
                    pass
            except Exception:
                pass  # address parse error — proceed and let ZMQ handle it

            self.rm = REManagerAPI(
                zmq_control_addr=ctrl_addr,
                zmq_info_addr=zmq_info or ZMQ_INFO,
            )
            status = self.rm.status()
            self.connected.emit()
            self.status_updated.emit(status)
            self._load_plans_devices()
            info_addr = zmq_info or ZMQ_INFO
            msg = self._console_mon.start(info_addr)
            self.console_updated.emit(f"[EasyBluesky] {msg}\n")
            self._doc_writer.start(zmq_doc or ZMQ_DOC_ADDR)
            return True
        except Exception as e:
            self.rm = None
            self.error_occurred.emit(f"Connection failed: {e}")
            self.disconnected.emit()
            return False
        finally:
            self._is_connecting = False

    def start_log_tail(self, settings: dict, log_file: str):
        """Start SSH log-file tailing for SSH-managed RE Manager instances."""
        self._ssh_settings = settings   # reused by fetch_scan_log
        msg = self._log_tailer.start(settings, log_file)
        self.console_updated.emit(f"[EasyBluesky] {msg}\n")

    def stop_log_tail(self):
        """Stop the SSH log tailer (call on disconnect or profile switch)."""
        self._log_tailer.stop()

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

        # Pass the devices file and MongoDB settings via environment variables
        env = dict(os.environ)
        env["EASY_BLUESKY_DEVICES_FILE"] = devices_file
        mongo_db   = profile.get("mongo_db",   "").strip()
        mongo_host = profile.get("mongo_host", "").strip() or "localhost"
        mongo_port = str(int(profile.get("mongo_port", 27017)))
        if mongo_db:
            env["EASY_BLUESKY_MONGO_DB"]   = mongo_db
            env["EASY_BLUESKY_MONGO_HOST"] = mongo_host
            env["EASY_BLUESKY_MONGO_PORT"] = mongo_port

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
            msg = str(e).lower()
            # Suppress transient states that resolve on the next poll cycle.
            if "environment is not open" not in msg and "must be in idle state" not in msg:
                self.error_occurred.emit(f"Failed to load plans/devices: {e}")

    def reload_plans_devices(self):
        """Schedule a plans/devices refresh on the next poll cycle.

        Setting a flag instead of calling _load_plans_devices() directly keeps
        all rm ZMQ calls on the poll thread and avoids socket conflicts when
        called from the main thread (e.g. after script_upload).
        """
        self._reload_plans_requested = True

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
        total, console_msgs = 0, 0
        types_seen: set = set()
        topics_seen: set = set()
        samples: list = []          # first 3 frames for inspection
        while time.monotonic() < deadline:
            try:
                parts = sock.recv_multipart()
                total += 1
                for frame in parts:
                    try:
                        obj = json.loads(frame)
                        if isinstance(obj, dict):
                            # Determine message type (Format A or B)
                            t = obj.get("type", "")
                            if not t:
                                inner = obj.get("msg")
                                if isinstance(inner, dict):
                                    if "console_output" in inner:
                                        t = "console_output"
                                    elif inner.get("type"):
                                        t = inner["type"]
                                    elif "status" in inner:
                                        t = "status"
                                    else:
                                        t = "(no type)"
                                else:
                                    t = "(no type)"
                            types_seen.add(t)
                            if t == "console_output":
                                console_msgs += 1
                            if len(samples) < 3:
                                samples.append(repr(frame[:120]))
                    except Exception:
                        if frame:
                            topics_seen.add(frame[:20].decode("utf-8", errors="replace"))
                            if len(samples) < 3:
                                samples.append(f"[topic] {frame[:40]!r}")
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
            if topics_seen:
                lines.append(f"    ZMQ topics seen:  {sorted(topics_seen)}\n")
            lines.append(f"    Message types:    {sorted(types_seen)}\n")
            if samples:
                lines.append("    Sample frames:\n")
                for s in samples:
                    lines.append(f"      {s}\n")
            if console_msgs:
                lines.append(f"    console_output messages: {console_msgs}\n")
            else:
                lines.append(
                    "    ✗ No console_output in this window (normal for idle env).\n"
                    "    → Open Env while the console is visible — startup\n"
                    "      messages from re_startup_mongo.py should appear live.\n"
                )
        return "".join(lines)


    def poll(self):
        _prev_env_state = None
        _opening_env    = False  # True after closed→executing_task

        while self._active:
            if self.rm and not self._is_connecting:
                if self._reload_plans_requested:
                    self._reload_plans_requested = False
                    self._load_plans_devices()
                try:
                    status  = self.rm.status()
                    self.status_updated.emit(status)
                    queue   = self.rm.queue_get()
                    history = self.rm.history_get()
                    self.queue_updated.emit(queue.get("items", []))
                    self.running_item_updated.emit(queue.get("running_item") or {})
                    self.history_updated.emit(history.get("items", []))

                    env_state = status.get("worker_environment_state", "")
                    if not env_state:
                        env_state = "idle" if status.get("worker_environment_exists") else "closed"

                    # environment_open() goes: closed → executing_task → idle.
                    # script_upload goes:      idle   → executing_task → idle.
                    # Only fire env_opened for the first case.
                    _OPEN_STATES = ("idle", "executing_plan", "paused")
                    _env_open = env_state in _OPEN_STATES
                    _was_open = _prev_env_state in _OPEN_STATES
                    _was_task = _prev_env_state == "executing_task"

                    if env_state == "executing_task" and _prev_env_state in (None, "closed"):
                        _opening_env = True
                    elif env_state == "closed":
                        _opening_env = False

                    just_opened = (
                        (_env_open and (not _was_open or _prev_env_state is None)) or
                        (_was_task and env_state == "idle" and _opening_env)
                    )

                    if just_opened:
                        _opening_env = False
                        self._load_plans_devices()
                        self.fetch_device_pvnames()
                        self.env_opened.emit()
                    elif _was_task and env_state == "idle":
                        # script_upload or other admin task finished
                        self._load_plans_devices()
                    elif env_state == "closed" and _was_open:
                        self.env_closed.emit()

                    _prev_env_state = env_state

                    # Drain both ZMQ subscriber and SSH log tailer
                    msgs = self._console_mon.drain() + self._log_tailer.drain()
                    if msgs:
                        self.console_updated.emit("".join(msgs))
                except Exception:
                    if not self._is_connecting:
                        self.rm = None
                        self.disconnected.emit()
            time.sleep(self._poll_interval)

    def set_doc_writer_exp_dir(self, path: str):
        """Tell the local doc writer where to save JSONL files."""
        self._doc_writer.set_exp_dir(path)

    def disconnect(self):
        """Drop the ZMQ connection immediately without stopping the poll loop."""
        self._console_mon.stop()
        self._log_tailer.stop()
        self._doc_writer.stop()
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

    def upload_scripts(self, scripts: list) -> list:
        """Upload multiple Python script strings via script_upload.

        Returns a list of (ok, msg) tuples, one per script.
        Stops on the first failure and fills remaining entries with (False, "skipped").
        """
        results = []
        for script in scripts:
            try:
                r = self.rm.script_upload(script=script)
                ok  = r.get("success", False)
                msg = r.get("msg", "")
                results.append((ok, msg))
                if not ok:
                    break
            except Exception as e:
                results.append((False, str(e)))
                break
        skipped = len(scripts) - len(results)
        results.extend([(False, "skipped")] * skipped)
        return results

    def _on_device_read_error(self, msg: str) -> None:
        _m = msg.lower()
        if "environment is not open" not in _m and "must be in idle" not in _m:
            self.error_occurred.emit(msg)

    def read_devices_status(self):
        """Submit read_devices_status() to the RE environment and emit device_readings_updated when done."""
        if self.rm is None:
            self.error_occurred.emit("Not connected — cannot read device status")
            return
        if self._device_reader is not None and self._device_reader.isRunning():
            return  # already in progress
        self._device_reader = _DeviceStatusReader(self.rm)
        self._device_reader.readings_ready.connect(self.device_readings_updated)
        self._device_reader.read_error.connect(self.device_read_error)
        self._device_reader.read_error.connect(self._on_device_read_error)
        self._device_reader.start()

    def set_sim_device(self, name: str, value: float):
        """Call set_sim_device() in the RE environment via a proper QThread that polls task_result."""
        if self.rm is None:
            return
        if self._sim_device_setter is not None and self._sim_device_setter.isRunning():
            # Previous set still in flight — drop this one to avoid stacking requests.
            return
        self._sim_device_setter = _SimDeviceSetter(self.rm, name, value)
        self._sim_device_setter.done.connect(
            lambda ok, msg: self.sim_device_set_done.emit(name, ok, msg)
        )
        self._sim_device_setter.start()

    def fetch_scan_log(self, remote_path: str):
        """Fetch scans_log.json from the remote machine via SFTP.

        Emits scan_log_ready(bytes) on success or scan_log_error(str) on failure.
        If SSH settings are not available (local/sim profile), emits scan_log_error.
        """
        if not self._ssh_settings.get("host"):
            self.scan_log_error.emit("No SSH host configured — cannot fetch remote scan log")
            return
        if self._scan_log_fetcher and self._scan_log_fetcher.isRunning():
            return   # already in flight
        self._scan_log_fetcher = _ScanLogFetcher(self._ssh_settings, remote_path)
        self._scan_log_fetcher.data_ready.connect(self.scan_log_ready)
        self._scan_log_fetcher.fetch_error.connect(self.scan_log_error)
        self._scan_log_fetcher.start()

    def reset_scan_id(self):
        """Reset the RunEngine scan_id counter to 0 (fire-and-forget background thread)."""
        if self.rm is None:
            return
        def _run():
            try:
                from bluesky_queueserver_api import BFunc
                self.rm.function_execute(item=BFunc("reset_scan_id"))
            except Exception:
                pass
        import threading
        threading.Thread(target=_run, daemon=True).start()

    def fetch_device_pvnames(self):
        """Fetch PV names for all devices from the RE environment and emit pv_names_ready."""
        if self.rm is None:
            return
        if self._pv_names_reader is not None and self._pv_names_reader.isRunning():
            return
        self._pv_names_reader = _PVNamesReader(self.rm)
        self._pv_names_reader.pv_names_ready.connect(self.pv_names_ready)
        self._pv_names_reader.read_error.connect(self.pv_names_error)
        self._pv_names_reader.start()
