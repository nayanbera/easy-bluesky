from bluesky.plan_stubs import (
    mv, sleep, trigger_and_read, move_per_step,
    trigger, read, open_run, close_run,
)
from bluesky.plans import (
    count, scan, rel_scan, list_scan, rel_list_scan,
    list_grid_scan, rel_list_grid_scan, grid_scan, rel_grid_scan,
)
import bluesky.preprocessors as bpp
import os
import numpy as np
import copy
from bluesky import plan_stubs as bps
from bluesky.suspenders import SuspendFloor
from ophyd import EpicsSignal, EpicsSignalRO, Device
from typing import Optional
import csv

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scan_num_from_log(exp_dir: str) -> int:
    """Return the next scan number by counting completed entries in scans_log.json.

    scans_log.json is written by re_startup_mongo after every run stop, so its
    length at plan-start time equals the number of scans already completed in
    this experiment — making len + 1 the correct sequential number for the next scan.
    """
    import json as _json
    log_path = os.path.join(exp_dir, "scans_log.json")
    try:
        with open(log_path, encoding="utf-8") as _f:
            entries = _json.load(_f)
        if isinstance(entries, list):
            return len(entries) + 1
    except Exception:
        pass
    return 1


def _exp_dir_from_md(md: dict) -> str:
    """Return the experiment directory path to use on the RE machine.

    Prefers remote_exp_dir (Linux-side NFS or detector-local path) over
    exp_dir (Mac-side local path), falling back to ~/.easy_bluesky/data.
    """
    return (
        md.get("remote_exp_dir")
        or md.get("exp_dir")
        or os.path.expanduser("~/.easy_bluesky/data")
    )


def _resolve_device(name_or_obj):
    """Coerce a device parameter to an actual Device object.

    bluesky-queueserver v0.0.25 may pass Optional[Device] parameters as the
    string "None" instead of Python None, or as a device-name string instead
    of the resolved object.  This helper normalises all three cases:

      None / "None" / ""  →  None   (skip shutter logic)
      "motor_name"        →  globals().get("motor_name") — works after
                              re_startup_mongo injects devices into this
                              module's namespace at startup.
      <Device object>     →  returned unchanged
    """
    if name_or_obj is None:
        return None
    if not isinstance(name_or_obj, str):
        return name_or_obj  # already a Device / Signal object
    if name_or_obj.lower() in ("none", "", "null"):
        return None
    return globals().get(name_or_obj)  # resolve device name string


def _save_and_set_det_mode(detectors, hdf_autosave: bool, saved: dict):
    """Save image_mode and hdf1.auto_save for each area detector, then set new values.

    Parameters
    ----------
    detectors :
        List of detector objects from the plan.
    hdf_autosave : bool
        True  → set hdf1.auto_save = 'Yes'
        False → set hdf1.auto_save = 'No'
    saved : dict
        Mutable dict populated with restore entries; passed to the _cleanup
        closure so originals are restored after the scan.
        Signal objects as keys → values restored via bps.mv.
        ('_stage_sigs', obj, attr) tuple keys → values restored directly into
        obj.stage_sigs[attr] (no bps.mv needed, not a CA put).
    """
    _autosave_str = 'Yes' if hdf_autosave else 'No'
    for det in detectors:
        # Save acquire_time for all detector types so _cleanup restores it after
        # set_detector_acquire_time changes it.
        if hasattr(det, 'cam') and hasattr(det.cam, 'acquire_time'):
            _orig = yield from bps.rd(det.cam.acquire_time)
            saved[det.cam.acquire_time] = _orig
            if hasattr(det.cam, 'acquire_period'):
                _orig = yield from bps.rd(det.cam.acquire_period)
                saved[det.cam.acquire_period] = _orig
        elif hasattr(det, 'preset_time'):
            _orig = yield from bps.rd(det.preset_time)
            saved[det.preset_time] = _orig
        elif hasattr(det, 'count_time'):
            _orig = yield from bps.rd(det.count_time)
            saved[det.count_time] = _orig
        elif hasattr(det, 'preset_real'):
            _orig = yield from bps.rd(det.preset_real)
            saved[det.preset_real] = _orig

        if not hasattr(det, 'cam'):
            continue

        # Patch ALL stage_sigs that reference image_mode — on both cam and hdf1.
        # Some HDF5Plugin subclasses carry 'parent.cam.image_mode': 'Multiple' in
        # their stage_sigs. Because det.hdf1 stages AFTER det.cam, that key overrides
        # cam.stage_sigs even if we patched it, putting image_mode back to Multiple.
        _subs = [det.cam] + ([det.hdf1] if hasattr(det, 'hdf1') else [])
        for _sub in _subs:
            _ss = getattr(_sub, 'stage_sigs', None)
            if not _ss:
                continue
            for _key in list(_ss.keys()):
                if 'image_mode' in (_key if isinstance(_key, str) else ''):
                    saved[('_stage_sigs', _sub, _key)] = _ss[_key]
                    _ss[_key] = 'Single'

        if hasattr(det.cam, 'image_mode'):
            orig = yield from bps.rd(det.cam.image_mode)
            saved[det.cam.image_mode] = orig
            yield from bps.mv(det.cam.image_mode, 'Single')

        if hasattr(det, 'hdf1') and hasattr(det.hdf1, 'auto_save'):
            _hdf_ss = getattr(det.hdf1, 'stage_sigs', {})
            if 'auto_save' in _hdf_ss:
                saved[('_stage_sigs', det.hdf1, 'auto_save')] = _hdf_ss['auto_save']
                det.hdf1.stage_sigs['auto_save'] = _autosave_str
            orig = yield from bps.rd(det.hdf1.auto_save)
            saved[det.hdf1.auto_save] = orig
            yield from bps.mv(det.hdf1.auto_save, _autosave_str)


# ---------------------------------------------------------------------------
# Beam-loss suspender
# ---------------------------------------------------------------------------

class RelativeBeamdownSuspenders:
    """Relative beam-loss suspender for use inside Bluesky plans.

    Suspend measurement if:
        reference signal readback < suspend_fraction * reference_value

    Resume measurement if:
        reference signal readback > resume_fraction * reference_value
    """

    def __init__(
        self,
        ref_pv,
        suspend_fraction=0.50,
        resume_fraction=0.80,
        beam_sleep=2,
        reference_value=None,
    ):
        self.ref_pv = ref_pv
        self.ref_signal = EpicsSignalRO(ref_pv, name="ref_signal")
        self.suspend_fraction = float(suspend_fraction)
        self.resume_fraction = float(resume_fraction)
        self.beam_sleep = beam_sleep
        self.reference_value = None
        self.suspend_threshold = None
        self.resume_threshold = None
        self.beam_suspender = None
        self._validate_fractions()
        if reference_value is not None:
            self.reference_value = float(reference_value)
            self._update_thresholds()
            self._build_suspender()

    def _validate_fractions(self):
        if not (0 < self.suspend_fraction < 1):
            raise ValueError(
                f"suspend_fraction must be between 0 and 1, got {self.suspend_fraction}"
            )
        if not (0 < self.resume_fraction <= 1):
            raise ValueError(
                f"resume_fraction must be between 0 and 1, got {self.resume_fraction}"
            )
        if self.resume_fraction <= self.suspend_fraction:
            raise ValueError(
                f"resume_fraction ({self.resume_fraction}) must be greater than "
                f"suspend_fraction ({self.suspend_fraction})"
            )

    def _update_thresholds(self):
        if self.reference_value is None:
            raise ValueError("reference_value is not set")
        if self.reference_value <= 0:
            raise ValueError(f"reference_value must be > 0, got {self.reference_value}")
        self.suspend_threshold = self.reference_value * self.suspend_fraction
        self.resume_threshold  = self.reference_value * self.resume_fraction

    def _build_suspender(self):
        if self.reference_value is None:
            raise ValueError("reference_value must be set before building suspender")
        self.beam_suspender = SuspendFloor(
            self.ref_signal,
            suspend_thresh=self.suspend_threshold,
            resume_thresh=self.resume_threshold,
            sleep=self.beam_sleep,
        )

    def configure(
        self,
        capture_reference=True,
        reference_value=None,
        suspend_fraction=None,
        resume_fraction=None,
    ):
        if suspend_fraction is not None:
            self.suspend_fraction = float(suspend_fraction)
        if resume_fraction is not None:
            self.resume_fraction = float(resume_fraction)
        self._validate_fractions()
        if reference_value is not None:
            self.reference_value = float(reference_value)
        elif capture_reference:
            self.reference_value = float(self.ref_signal.get())
        if self.reference_value is None:
            raise ValueError(
                "No reference_value available. Use capture_reference=True "
                "or provide reference_value=..."
            )
        self._update_thresholds()
        self._build_suspender()

    def configure_from_current(self, suspend_fraction=None, resume_fraction=None):
        self.configure(
            capture_reference=True,
            suspend_fraction=suspend_fraction,
            resume_fraction=resume_fraction,
        )

    def install_plan(self):
        if self.beam_suspender is None:
            raise RuntimeError("Suspender is not configured. Call configure(...) first.")
        yield from bps.install_suspender(self.beam_suspender)

    def remove_plan(self):
        if self.beam_suspender is not None:
            yield from bps.remove_suspender(self.beam_suspender)

    def get_current_ref(self):
        return float(self.ref_signal.get())

    def status(self):
        print(f"ref_pv              = {self.ref_pv}")
        print(f"ref_signal current  = {self.get_current_ref()}")
        print(f"reference_value     = {self.reference_value}")
        print(f"suspend_fraction    = {self.suspend_fraction}")
        print(f"resume_fraction     = {self.resume_fraction}")
        print(f"suspend_threshold   = {self.suspend_threshold}")
        print(f"resume_threshold    = {self.resume_threshold}")


# ---------------------------------------------------------------------------
# Detector helpers
# ---------------------------------------------------------------------------

def set_detector_acquire_time(det, time: float):
    """Set acquire time for a detector (area detector, scaler, or XRF)."""
    if hasattr(det, "cam") and hasattr(det.cam, "acquire_time"):
        yield from mv(det.cam.acquire_time, time)
        if hasattr(det.cam, "acquire_period"):
            yield from mv(det.cam.acquire_period, time + 0.01)
    elif hasattr(det, "preset_time"):
        yield from mv(det.preset_time, time)
    elif hasattr(det, "count_time"):
        yield from mv(det.count_time, time)
    elif hasattr(det, "preset_real"):
        yield from mv(det.preset_real, time)
    else:
        print(f"Detector {det.name} has no recognised acquire-time attribute")


def set_areadetector_hdf(det, exp_dir: str, sample_name: str, scan_num: int):
    """Configure HDF file path and name for an area detector.

    Parameters
    ----------
    det         : area detector ophyd object
    exp_dir     : experiment directory on the RE machine (remote_exp_dir preferred)
    sample_name : sample name used for subfolder and file naming
    scan_num    : scan number used to form the filename suffix (_S_NNNN)
    """
    if not sample_name:
        raise ValueError("sample_name must not be empty")

    if det.name == "dante":
        local_path = f"{exp_dir}/{sample_name}/{det.name}/"
        file_path  = f"/local/home/dpuser{local_path.replace('/home/chem_epics/', '/')}"
        os.makedirs(local_path, exist_ok=True)
        det.filename.put(sample_name + f"_S_{scan_num:04d}")
        det.filepath.put(file_path)

    elif det.name == "Pil300K":
        local_path = f"{exp_dir}/{sample_name}/{det.name}/"
        file_path  = local_path.replace("chem_epics", "det")
        os.makedirs(local_path, exist_ok=True)
        yield from mv(det.hdf1.file_path, file_path)
        yield from mv(det.hdf1.file_name, sample_name + f"_S_{scan_num:04d}")

    else:
        if hasattr(det, "hdf1"):
            file_path = os.path.join(exp_dir, sample_name, det.name)
            os.makedirs(file_path, exist_ok=True)
            yield from mv(det.hdf1.file_path, file_path)
            yield from mv(det.hdf1.file_name, sample_name + f"_S_{scan_num:04d}")


def _set_image_mode_single(detectors):
    """Set image_mode='Single' on all cam-equipped detectors.

    Called at the start of each per_step / per_shot function, after staging
    has already run.  Some FileStoreHDF5 / plugin stage() implementations
    directly put 'Multiple' to parent.cam.image_mode (not via stage_sigs),
    so stage_sigs patching alone is insufficient.  Setting it here, before
    every trigger, is the reliable override — the extra CA put is negligible.
    """
    for _det in detectors:
        if hasattr(_det, 'cam') and hasattr(_det.cam, 'image_mode'):
            yield from bps.mv(_det.cam.image_mode, 'Single')


# ---------------------------------------------------------------------------
# Scan plans
# ---------------------------------------------------------------------------

def count_w_time(
    detectors,
    num: int,
    delay: float = 0.0,
    exposure_time: float = 1.0,
    *,
    shutter: Optional[Device] = None,
    hdf_autosave: bool = True,
    md: dict = None,
    **kwargs,
):
    """Count with per-shot exposure time and optional shutter control.

    Parameters
    ----------
    detectors :
        Detectors to read.
    num : int
        Number of acquisitions.
    delay : float
        Wait between acquisitions in seconds.
    exposure_time : float
        Detector exposure time in seconds.
    shutter : Device, optional
        Fast shutter device — opened before each trigger, closed after.
    hdf_autosave : bool
        True (default) saves HDF files; False disables auto_save.
    md : dict, optional
        Additional metadata (experiment fields auto-injected by EasyBluesky).
    """
    md = md or {}
    shutter = _resolve_device(shutter)
    _dir    = _exp_dir_from_md(md)
    _sample = md.get("sample_name", "sample")
    _scan_n = _scan_num_from_log(_dir)

    def per_shot(detectors):
        yield from _set_image_mode_single(detectors)
        if shutter is not None:
            yield from mv(shutter, 0)
            yield from sleep(0.3)
        yield from trigger_and_read(detectors)
        if shutter is not None:
            yield from mv(shutter, 1)
        yield from sleep(delay)

    saved = {}

    def _body():
        yield from _save_and_set_det_mode(detectors, hdf_autosave, saved)
        for detector in detectors:
            yield from set_detector_acquire_time(detector, exposure_time)
            yield from set_areadetector_hdf(detector, _dir, _sample, _scan_n)
        kwargs.setdefault("per_shot", per_shot)
        yield from count(detectors, num=num, delay=delay, md=md, **kwargs)

    def _cleanup():
        for key, val in saved.items():
            if isinstance(key, tuple) and len(key) == 3 and key[0] == '_stage_sigs':
                _, obj, attr = key
                obj.stage_sigs[attr] = val
            else:
                yield from bps.mv(key, val)

    yield from bpp.finalize_wrapper(_body(), _cleanup())


def scan_w_time_n_delay(
    detectors,
    *args,
    num: int,
    acquire_time: float = 1.0,
    delay: float = 0.0,
    shutter: Optional[Device] = None,
    hdf_autosave: bool = True,
    md: dict = None,
    **kwargs,
):
    """Absolute scan with per-step acquire time, delay, and optional shutter.

    Parameters
    ----------
    detectors :
        Detectors to read at each step.
    *args : motor start stop [motor start stop ...]
        Motors and absolute start/stop positions.
    num : int
        Number of points in the scan.
    acquire_time : float
        Detector exposure time in seconds.
    delay : float
        Extra wait after each step in seconds.
    shutter : Device, optional
        Fast shutter device.
    hdf_autosave : bool
        True (default) saves HDF files; False disables auto_save.
    md : dict, optional
        Additional metadata (experiment fields auto-injected by EasyBluesky).
    """
    md = md or {}
    shutter = _resolve_device(shutter)
    _dir    = _exp_dir_from_md(md)
    _sample = md.get("sample_name", "sample")
    _scan_n = _scan_num_from_log(_dir)

    def one_nd_step_with_delay(detectors, step, pos_cache):
        yield from _set_image_mode_single(detectors)
        yield from move_per_step(step, pos_cache)
        if shutter is not None:
            yield from mv(shutter, 0)
            yield from sleep(0.3)
        yield from trigger_and_read(list(detectors) + list(step.keys()))
        if shutter is not None:
            yield from mv(shutter, 1)
        yield from sleep(delay)

    saved = {}

    def _body():
        yield from _save_and_set_det_mode(detectors, hdf_autosave, saved)
        for detector in detectors:
            yield from set_detector_acquire_time(detector, acquire_time)
            yield from set_areadetector_hdf(detector, _dir, _sample, _scan_n)
        kwargs.setdefault("per_step", one_nd_step_with_delay)
        yield from scan(detectors, *args, num=num, md=md, **kwargs)

    def _cleanup():
        for key, val in saved.items():
            if isinstance(key, tuple) and len(key) == 3 and key[0] == '_stage_sigs':
                _, obj, attr = key
                obj.stage_sigs[attr] = val
            else:
                yield from bps.mv(key, val)

    yield from bpp.finalize_wrapper(_body(), _cleanup())


def rel_scan_w_time_n_delay(
    detectors,
    *args,
    num: int,
    acquire_time: float = 1.0,
    delay: float = 0.0,
    shutter: Optional[Device] = None,
    hdf_autosave: bool = True,
    md: dict = None,
    **kwargs,
):
    """Relative scan with per-step acquire time, delay, and optional shutter.

    Parameters
    ----------
    detectors :
        Detectors to read at each step.
    *args : motor start stop [motor start stop ...]
        Motors and relative start/stop positions from current position.
    num : int
        Number of points in the scan.
    acquire_time : float
        Detector exposure time in seconds.
    delay : float
        Extra wait after each step in seconds.
    shutter : Device, optional
        Fast shutter device.
    hdf_autosave : bool
        True (default) saves HDF files; False disables auto_save.
    md : dict, optional
        Additional metadata (experiment fields auto-injected by EasyBluesky).
    """
    md = md or {}
    shutter = _resolve_device(shutter)
    _dir    = _exp_dir_from_md(md)
    _sample = md.get("sample_name", "sample")
    _scan_n = _scan_num_from_log(_dir)

    def one_nd_step_with_delay(detectors, step, pos_cache):
        yield from _set_image_mode_single(detectors)
        yield from move_per_step(step, pos_cache)
        if shutter is not None:
            yield from mv(shutter, 0)
            yield from sleep(0.3)
        yield from trigger_and_read(list(detectors) + list(step.keys()))
        if shutter is not None:
            yield from mv(shutter, 1)
        yield from sleep(delay)

    saved = {}

    def _body():
        yield from _save_and_set_det_mode(detectors, hdf_autosave, saved)
        for detector in detectors:
            yield from set_detector_acquire_time(detector, acquire_time)
            yield from set_areadetector_hdf(detector, _dir, _sample, _scan_n)
        kwargs.setdefault("per_step", one_nd_step_with_delay)
        yield from rel_scan(detectors, *args, num=num, md=md, **kwargs)

    def _cleanup():
        for key, val in saved.items():
            if isinstance(key, tuple) and len(key) == 3 and key[0] == '_stage_sigs':
                _, obj, attr = key
                obj.stage_sigs[attr] = val
            else:
                yield from bps.mv(key, val)

    yield from bpp.finalize_wrapper(_body(), _cleanup())


def grid_scan_w_time_n_delay(
    detectors,
    *args,
    acquire_time: float = 1.0,
    delay: float = 0.0,
    shutter: Optional[Device] = None,
    hdf_autosave: bool = True,
    md: dict = None,
    **kwargs,
):
    """Grid scan with per-step acquire time, delay, and optional shutter.

    Parameters
    ----------
    detectors :
        Detectors to read at each step.
    *args : motor start stop num [snake] ...
        Motors, ranges, and point counts per axis (standard grid_scan args).
    acquire_time : float
        Detector exposure time in seconds.
    delay : float
        Extra wait after each step in seconds.
    shutter : Device, optional
        Fast shutter device.
    hdf_autosave : bool
        True (default) saves HDF files; False disables auto_save.
    md : dict, optional
        Additional metadata (experiment fields auto-injected by EasyBluesky).
    """
    md = md or {}
    shutter = _resolve_device(shutter)
    _dir    = _exp_dir_from_md(md)
    _sample = md.get("sample_name", "sample")
    _scan_n = _scan_num_from_log(_dir)

    def one_nd_step_with_delay(detectors, step, pos_cache):
        yield from _set_image_mode_single(detectors)
        yield from move_per_step(step, pos_cache)
        if shutter is not None:
            yield from mv(shutter, 0)
            yield from sleep(0.3)
        yield from trigger_and_read(list(detectors) + list(step.keys()))
        if shutter is not None:
            yield from mv(shutter, 1)
        yield from sleep(delay)

    saved = {}

    def _body():
        yield from _save_and_set_det_mode(detectors, hdf_autosave, saved)
        for detector in detectors:
            yield from set_detector_acquire_time(detector, acquire_time)
            yield from set_areadetector_hdf(detector, _dir, _sample, _scan_n)
        kwargs.setdefault("per_step", one_nd_step_with_delay)
        yield from grid_scan(detectors, *args, md=md, **kwargs)

    def _cleanup():
        for key, val in saved.items():
            if isinstance(key, tuple) and len(key) == 3 and key[0] == '_stage_sigs':
                _, obj, attr = key
                obj.stage_sigs[attr] = val
            else:
                yield from bps.mv(key, val)

    yield from bpp.finalize_wrapper(_body(), _cleanup())


def rel_grid_scan_w_time_n_delay(
    detectors,
    *args,
    acquire_time: float = 1.0,
    delay: float = 0.0,
    shutter: Optional[Device] = None,
    hdf_autosave: bool = True,
    md: dict = None,
    **kwargs,
):
    """Relative grid scan with per-step acquire time, delay, and optional shutter.

    Parameters
    ----------
    detectors :
        Detectors to read at each step.
    *args : motor start stop num [snake] ...
        Motors, relative ranges, and point counts per axis.
    acquire_time : float
        Detector exposure time in seconds.
    delay : float
        Extra wait after each step in seconds.
    shutter : Device, optional
        Fast shutter device.
    hdf_autosave : bool
        True (default) saves HDF files; False disables auto_save.
    md : dict, optional
        Additional metadata (experiment fields auto-injected by EasyBluesky).
    """
    md = md or {}
    shutter = _resolve_device(shutter)
    _dir    = _exp_dir_from_md(md)
    _sample = md.get("sample_name", "sample")
    _scan_n = _scan_num_from_log(_dir)

    def one_nd_step_with_delay(detectors, step, pos_cache):
        yield from _set_image_mode_single(detectors)
        yield from move_per_step(step, pos_cache)
        if shutter is not None:
            yield from mv(shutter, 0)
            yield from sleep(0.3)
        yield from trigger_and_read(list(detectors) + list(step.keys()))
        if shutter is not None:
            yield from mv(shutter, 1)
        yield from sleep(delay)

    saved = {}

    def _body():
        yield from _save_and_set_det_mode(detectors, hdf_autosave, saved)
        for detector in detectors:
            yield from set_detector_acquire_time(detector, acquire_time)
            yield from set_areadetector_hdf(detector, _dir, _sample, _scan_n)
        kwargs.setdefault("per_step", one_nd_step_with_delay)
        yield from rel_grid_scan(detectors, *args, md=md, **kwargs)

    def _cleanup():
        for key, val in saved.items():
            if isinstance(key, tuple) and len(key) == 3 and key[0] == '_stage_sigs':
                _, obj, attr = key
                obj.stage_sigs[attr] = val
            else:
                yield from bps.mv(key, val)

    yield from bpp.finalize_wrapper(_body(), _cleanup())


def list_scan_w_time_n_delay(
    detectors,
    *args,
    acquire_time: float = 1.0,
    delay: float = 0.0,
    shutter: Optional[Device] = None,
    hdf_autosave: bool = True,
    md: dict = None,
    **kwargs,
):
    """List scan with per-step acquire time, delay, and optional shutter.

    Parameters
    ----------
    detectors :
        Detectors to read at each step.
    *args : motor positions [motor positions ...]
        Motors and their position lists (standard list_scan args).
    acquire_time : float
        Detector exposure time in seconds.
    delay : float
        Extra wait after each step in seconds.
    shutter : Device, optional
        Fast shutter device.
    hdf_autosave : bool
        True (default) saves HDF files; False disables auto_save.
    md : dict, optional
        Additional metadata (experiment fields auto-injected by EasyBluesky).
    """
    md = md or {}
    shutter = _resolve_device(shutter)
    _dir    = _exp_dir_from_md(md)
    _sample = md.get("sample_name", "sample")
    _scan_n = _scan_num_from_log(_dir)

    def one_nd_step_with_delay(detectors, step, pos_cache):
        yield from _set_image_mode_single(detectors)
        yield from move_per_step(step, pos_cache)
        if shutter is not None:
            yield from mv(shutter, 0)
            yield from sleep(0.3)
        yield from trigger_and_read(list(detectors) + list(step.keys()))
        if shutter is not None:
            yield from mv(shutter, 1)
        yield from sleep(delay)

    saved = {}

    def _body():
        yield from _save_and_set_det_mode(detectors, hdf_autosave, saved)
        for detector in detectors:
            yield from set_detector_acquire_time(detector, acquire_time)
            yield from set_areadetector_hdf(detector, _dir, _sample, _scan_n)
        kwargs.setdefault("per_step", one_nd_step_with_delay)
        yield from list_scan(detectors, *args, md=md, **kwargs)

    def _cleanup():
        for key, val in saved.items():
            if isinstance(key, tuple) and len(key) == 3 and key[0] == '_stage_sigs':
                _, obj, attr = key
                obj.stage_sigs[attr] = val
            else:
                yield from bps.mv(key, val)

    yield from bpp.finalize_wrapper(_body(), _cleanup())


def rel_list_scan_w_time_n_delay(
    detectors,
    *args,
    acquire_time: float = 1.0,
    delay: float = 0.0,
    shutter: Optional[Device] = None,
    hdf_autosave: bool = True,
    md: dict = None,
    **kwargs,
):
    """Relative list scan with per-step acquire time, delay, and optional shutter.

    Parameters
    ----------
    detectors :
        Detectors to read at each step.
    *args : motor positions [motor positions ...]
        Motors and their relative position lists.
    acquire_time : float
        Detector exposure time in seconds.
    delay : float
        Extra wait after each step in seconds.
    shutter : Device, optional
        Fast shutter device.
    hdf_autosave : bool
        True (default) saves HDF files; False disables auto_save.
    md : dict, optional
        Additional metadata (experiment fields auto-injected by EasyBluesky).
    """
    md = md or {}
    shutter = _resolve_device(shutter)
    _dir    = _exp_dir_from_md(md)
    _sample = md.get("sample_name", "sample")
    _scan_n = _scan_num_from_log(_dir)

    def one_nd_step_with_delay(detectors, step, pos_cache):
        yield from _set_image_mode_single(detectors)
        yield from move_per_step(step, pos_cache)
        if shutter is not None:
            yield from mv(shutter, 0)
            yield from sleep(0.3)
        yield from trigger_and_read(list(detectors) + list(step.keys()))
        if shutter is not None:
            yield from mv(shutter, 1)
        yield from sleep(delay)

    saved = {}

    def _body():
        yield from _save_and_set_det_mode(detectors, hdf_autosave, saved)
        for detector in detectors:
            yield from set_detector_acquire_time(detector, acquire_time)
            yield from set_areadetector_hdf(detector, _dir, _sample, _scan_n)
        kwargs.setdefault("per_step", one_nd_step_with_delay)
        yield from rel_list_scan(detectors, *args, md=md, **kwargs)

    def _cleanup():
        for key, val in saved.items():
            if isinstance(key, tuple) and len(key) == 3 and key[0] == '_stage_sigs':
                _, obj, attr = key
                obj.stage_sigs[attr] = val
            else:
                yield from bps.mv(key, val)

    yield from bpp.finalize_wrapper(_body(), _cleanup())


def list_grid_scan_w_time_n_delay(
    detectors,
    *args,
    acquire_time: float = 1.0,
    delay: float = 0.0,
    shutter: Optional[Device] = None,
    hdf_autosave: bool = True,
    md: dict = None,
    **kwargs,
):
    """List grid scan with per-step acquire time, delay, and optional shutter.

    Parameters
    ----------
    detectors :
        Detectors to read at each step.
    *args : motor positions [snake] ...
        Motors and position lists per axis (standard list_grid_scan args).
    acquire_time : float
        Detector exposure time in seconds.
    delay : float
        Extra wait after each step in seconds.
    shutter : Device, optional
        Fast shutter device.
    hdf_autosave : bool
        True (default) saves HDF files; False disables auto_save.
    md : dict, optional
        Additional metadata (experiment fields auto-injected by EasyBluesky).
    """
    md = md or {}
    shutter = _resolve_device(shutter)
    _dir    = _exp_dir_from_md(md)
    _sample = md.get("sample_name", "sample")
    _scan_n = _scan_num_from_log(_dir)

    def one_nd_step_with_delay(detectors, step, pos_cache):
        yield from _set_image_mode_single(detectors)
        yield from move_per_step(step, pos_cache)
        if shutter is not None:
            yield from mv(shutter, 0)
            yield from sleep(0.3)
        yield from trigger_and_read(list(detectors) + list(step.keys()))
        if shutter is not None:
            yield from mv(shutter, 1)
        yield from sleep(delay)

    saved = {}

    def _body():
        yield from _save_and_set_det_mode(detectors, hdf_autosave, saved)
        for detector in detectors:
            yield from set_detector_acquire_time(detector, acquire_time)
            yield from set_areadetector_hdf(detector, _dir, _sample, _scan_n)
        kwargs.setdefault("per_step", one_nd_step_with_delay)
        yield from list_grid_scan(detectors, *args, md=md, **kwargs)

    def _cleanup():
        for key, val in saved.items():
            if isinstance(key, tuple) and len(key) == 3 and key[0] == '_stage_sigs':
                _, obj, attr = key
                obj.stage_sigs[attr] = val
            else:
                yield from bps.mv(key, val)

    yield from bpp.finalize_wrapper(_body(), _cleanup())


def rel_list_grid_scan_w_time_n_delay(
    detectors,
    *args,
    acquire_time: float = 1.0,
    delay: float = 0.0,
    shutter: Optional[Device] = None,
    hdf_autosave: bool = True,
    md: dict = None,
    **kwargs,
):
    """Relative list grid scan with per-step acquire time, delay, and optional shutter.

    Parameters
    ----------
    detectors :
        Detectors to read at each step.
    *args : motor positions [snake] ...
        Motors and relative position lists per axis.
    acquire_time : float
        Detector exposure time in seconds.
    delay : float
        Extra wait after each step in seconds.
    shutter : Device, optional
        Fast shutter device.
    hdf_autosave : bool
        True (default) saves HDF files; False disables auto_save.
    md : dict, optional
        Additional metadata (experiment fields auto-injected by EasyBluesky).
    """
    md = md or {}
    shutter = _resolve_device(shutter)
    _dir    = _exp_dir_from_md(md)
    _sample = md.get("sample_name", "sample")
    _scan_n = _scan_num_from_log(_dir)

    def one_nd_step_with_delay(detectors, step, pos_cache):
        yield from _set_image_mode_single(detectors)
        yield from move_per_step(step, pos_cache)
        if shutter is not None:
            yield from mv(shutter, 0)
            yield from sleep(0.3)
        yield from trigger_and_read(list(detectors) + list(step.keys()))
        if shutter is not None:
            yield from mv(shutter, 1)
        yield from sleep(delay)

    saved = {}

    def _body():
        yield from _save_and_set_det_mode(detectors, hdf_autosave, saved)
        for detector in detectors:
            yield from set_detector_acquire_time(detector, acquire_time)
            yield from set_areadetector_hdf(detector, _dir, _sample, _scan_n)
        kwargs.setdefault("per_step", one_nd_step_with_delay)
        yield from rel_list_grid_scan(detectors, *args, md=md, **kwargs)

    def _cleanup():
        for key, val in saved.items():
            if isinstance(key, tuple) and len(key) == 3 and key[0] == '_stage_sigs':
                _, obj, attr = key
                obj.stage_sigs[attr] = val
            else:
                yield from bps.mv(key, val)

    yield from bpp.finalize_wrapper(_body(), _cleanup())


# ---------------------------------------------------------------------------
# CSV multi-motor list scan
# ---------------------------------------------------------------------------

def load_multi_motor_csv(filename):
    with open(filename, "r") as f:
        reader = csv.reader(f)
        header = next(reader)
        columns = [[] for _ in range(len(header))]
        for row in reader:
            if not row:
                continue
            for i in range(len(header)):
                columns[i].append(float(row[i]))
    return header, columns


def list_scan_w_time_n_delay_from_csv(
    detectors,
    motors,
    csv_file: str,
    acquire_time: float = 1.0,
    delay: float = 0.0,
    shutter: Optional[Device] = None,
    hdf_autosave: bool = True,
    md: dict = None,
    **kwargs,
):
    """Multi-motor list scan using positions loaded from a CSV file.

    Parameters
    ----------
    detectors :
        Detectors to read at each step.
    motors :
        List of motor objects (must match the number of CSV columns).
    csv_file : str
        Path to CSV file.  Each column is one motor; rows are scan points.
        First row is a header (column names are ignored).
    acquire_time : float
        Detector exposure time in seconds.
    delay : float
        Extra wait after each step in seconds.
    shutter : Device, optional
        Fast shutter device.
    hdf_autosave : bool
        True (default) saves HDF files; False disables auto_save.
    md : dict, optional
        Additional metadata (experiment fields auto-injected by EasyBluesky).
    """
    md = md or {}
    shutter = _resolve_device(shutter)
    _dir    = _exp_dir_from_md(md)
    _sample = md.get("sample_name", "sample")
    _scan_n = _scan_num_from_log(_dir)

    header, columns = load_multi_motor_csv(csv_file)
    if len(motors) != len(columns):
        raise ValueError(
            f"CSV has {len(columns)} columns but {len(motors)} motors were provided"
        )

    def one_nd_step_with_delay(detectors, step, pos_cache):
        yield from _set_image_mode_single(detectors)
        yield from move_per_step(step, pos_cache)
        if shutter is not None:
            yield from mv(shutter, 0)
            yield from sleep(0.3)
        yield from trigger_and_read(list(detectors) + list(step.keys()))
        if shutter is not None:
            yield from mv(shutter, 1)
        yield from sleep(delay)

    saved = {}

    def _body():
        yield from _save_and_set_det_mode(detectors, hdf_autosave, saved)
        for detector in detectors:
            yield from set_detector_acquire_time(detector, acquire_time)
            yield from set_areadetector_hdf(detector, _dir, _sample, _scan_n)
        kwargs.setdefault("per_step", one_nd_step_with_delay)
        scan_args = []
        for motor, pos_list in zip(motors, columns):
            scan_args.extend([motor, pos_list])
        yield from list_scan(detectors, *scan_args, md=md, **kwargs)

    def _cleanup():
        for key, val in saved.items():
            if isinstance(key, tuple) and len(key) == 3 and key[0] == '_stage_sigs':
                _, obj, attr = key
                obj.stage_sigs[attr] = val
            else:
                yield from bps.mv(key, val)

    yield from bpp.finalize_wrapper(_body(), _cleanup())


# ---------------------------------------------------------------------------
# Energy nested scans
# ---------------------------------------------------------------------------

def energy_nested_scan(
    energy_motor,
    energy_values,
    inner_plan,
    *inner_args,
    undulator=None,
    md: dict = None,
    inner_kwargs: dict = None,
):
    """Run an inner plan at each energy point.

    Parameters
    ----------
    energy_motor :
        Motor to move for energy (e.g. mono).
    energy_values :
        Iterable of absolute energy values.
    inner_plan :
        Bluesky plan function to call at each energy.
    *inner_args :
        Positional arguments forwarded to inner_plan.
    undulator : Device, optional
        Undulator motor, moved in tandem with energy_motor.
    md : dict, optional
        Metadata forwarded to inner_plan.
    inner_kwargs : dict, optional
        Keyword arguments forwarded to inner_plan.
    """
    md = md or {}
    inner_kwargs = dict(inner_kwargs or {})
    for energy in energy_values:
        yield from mv(energy_motor, energy)
        if undulator is not None:
            yield from mv(undulator, energy)
        inner_kwargs["md"] = md
        yield from inner_plan(*inner_args, **inner_kwargs)


def energy_nested_scan_relative(
    energy_motor,
    center: float,
    energy_range: float,
    step: float,
    inner_plan,
    *inner_args,
    sleep_time: float = 0.0,
    undulator=None,
    md: dict = None,
    inner_kwargs: dict = None,
):
    """Relative energy scan around a center value, running inner_plan at each point.

    Parameters
    ----------
    energy_motor :
        Motor to scan for energy (e.g. mono).
    center : float
        Central energy value.
    energy_range : float
        Total span around center (center ± energy_range/2).
    step : float
        Step size between energy points.
    inner_plan :
        Bluesky plan function to call at each energy.
    *inner_args :
        Positional arguments forwarded to inner_plan.
    sleep_time : float
        Sleep after each energy move (seconds).  First move gets 10x.
    undulator : Device, optional
        Undulator motor, moved in tandem with energy_motor.
    md : dict, optional
        Metadata forwarded to inner_plan.
    inner_kwargs : dict, optional
        Keyword arguments forwarded to inner_plan.
    """
    md = md or {}
    inner_kwargs = dict(inner_kwargs or {})

    start = center - energy_range / 2
    stop  = center + energy_range / 2
    energy_values = np.arange(start, stop, energy_range / step)
    initial_energy = energy_motor.user_readback.get()

    try:
        inner_kwargs["md"] = md
        for i, energy in enumerate(energy_values):
            yield from mv(energy_motor, energy)
            print(f"*** Set energy to {energy} keV ***")
            if undulator is not None:
                yield from mv(undulator, energy)
            yield from sleep(10 * sleep_time if i < 1 else sleep_time)
            yield from inner_plan(*inner_args, **inner_kwargs)
    finally:
        yield from mv(energy_motor, initial_energy)
        if undulator is not None:
            yield from mv(undulator, initial_energy)


# ---------------------------------------------------------------------------
# XRF energy scan
# ---------------------------------------------------------------------------

def energy_xrf_scan(
    energy_motor,
    center: float,
    energy_range: float,
    step: float,
    xrf_detectors,
    det_exposure_times: float = 1.0,
    undulator=None,
    sleep_time: float = 0.0,
    md: dict = None,
):
    """Relative energy scan with XRF measurement at each point.

    Parameters
    ----------
    energy_motor :
        Motor to scan for energy (e.g. mono).
    center : float
        Central energy value.
    energy_range : float
        Total span around center (center ± energy_range/2).
    step : float
        Step size between energy points.
    xrf_detectors :
        List of XRF detector objects.
    det_exposure_times : float
        Exposure time applied to all XRF detectors.
    undulator : Device, optional
        Undulator motor, moved in tandem with energy_motor.
    sleep_time : float
        Sleep after each energy move.  First move gets 10x.
    md : dict, optional
        Additional metadata (experiment fields auto-injected by EasyBluesky).
    """
    md = md or {}

    start = center - energy_range / 2
    stop  = center + energy_range / 2
    energy_values = np.arange(start, stop + step, step)

    yield from open_run(md=md)
    initial_energy = energy_motor.user_readback.get()

    try:
        for det in xrf_detectors:
            yield from set_detector_acquire_time(det, det_exposure_times)

        for i, energy in enumerate(energy_values):
            yield from mv(energy_motor, energy)
            if undulator is not None:
                yield from mv(undulator, energy)
            yield from sleep(10 * sleep_time if i < 1 else sleep_time)
            yield from trigger_and_read(list(xrf_detectors) + [energy_motor])
    finally:
        yield from mv(energy_motor, initial_energy)
        if undulator is not None:
            yield from mv(undulator, initial_energy)
        yield from close_run()
        print("Energy XRF scan complete, motors returned to initial positions.")


# ---------------------------------------------------------------------------
# ASWAXS energy scan
# ---------------------------------------------------------------------------

def aswaxs_energy_scan(
    energy_motor,
    energy_list,
    coord_motor_list,
    coord_list,
    detector_list,
    num_frame: int = 1,
    exposure_time: float = 1.0,
    delay_time: float = 1.0,
    sleep_time: float = 0.0,
    shutter: Optional[Device] = None,
    undulator=None,
    hdf_autosave: bool = True,
    md: dict = None,
):
    """Absolute energy scan: move to each energy, sweep positions, take frames.

    Parameters
    ----------
    energy_motor :
        Motor to scan for energy (e.g. mono).
    energy_list :
        List of absolute energies.
    coord_motor_list :
        List of position motors (x, y, z, …).
    coord_list :
        List of coordinate tuples, one per position point.
    detector_list :
        Detectors to read at each position.
    num_frame : int
        Number of frames per position.
    exposure_time : float
        Exposure time for all detectors.
    delay_time : float
        Delay between consecutive frames at one position.
    sleep_time : float
        Sleep after energy motor moves.  First and last move get 10x.
    shutter : Device, optional
        Fast shutter device.
    hdf_autosave : bool
        True (default) saves HDF files; False disables auto_save.
    undulator : Device, optional
        Undulator motor, moved in tandem with energy_motor.
    md : dict, optional
        Additional metadata (experiment fields auto-injected by EasyBluesky).
    """
    md = md or {}
    shutter = _resolve_device(shutter)
    _dir    = _exp_dir_from_md(md)
    _sample = md.get("sample_name", "sample")
    _scan_n = _scan_num_from_log(_dir)

    saved = {}

    def _body():
        yield from _save_and_set_det_mode(detector_list, hdf_autosave, saved)

        yield from open_run(md=md)
        initial_energy = energy_motor.user_readback.get()
        initial_coords = [m.user_readback.get() for m in coord_motor_list]

        beamdown = RelativeBeamdownSuspenders(ref_pv="15IDC:userTran10.E")

        for detector in detector_list:
            yield from set_detector_acquire_time(detector, exposure_time)
            yield from set_areadetector_hdf(detector, _dir, _sample, _scan_n)
        for detector in detector_list:
            yield from bps.stage(detector)
        # Override image_mode after staging — stage() may put 'Multiple' directly.
        for detector in detector_list:
            if hasattr(detector, 'cam') and hasattr(detector.cam, 'image_mode'):
                yield from bps.mv(detector.cam.image_mode, 'Single')

        try:
            for j, energy in enumerate(energy_list):
                yield from mv(energy_motor, energy)
                print(f"*** Set energy to {energy} keV ***")
                if undulator is not None:
                    yield from mv(undulator, energy)
                yield from sleep(
                    sleep_time * 10 if (j < 1 or j == len(energy_list) - 1) else sleep_time
                )

                beamdown.configure(capture_reference=True, suspend_fraction=0.50, resume_fraction=0.80)
                beamdown.status()
                yield from beamdown.install_plan()

                try:
                    for pos in coord_list:
                        for motor, coord in zip(coord_motor_list, pos):
                            yield from mv(motor, coord)
                        for i in range(num_frame):
                            yield from bps.checkpoint()
                            if shutter is not None:
                                yield from mv(shutter, 0)
                            yield from sleep(0.3)
                            yield from trigger_and_read(list(detector_list))
                            if shutter is not None:
                                yield from mv(shutter, 1)
                            if i < num_frame - 1:
                                yield from sleep(delay_time)
                finally:
                    yield from beamdown.remove_plan()
        finally:
            for detector in detector_list:
                yield from bps.unstage(detector)
            yield from mv(energy_motor, initial_energy)
            if undulator is not None:
                yield from mv(undulator, initial_energy)
            for motor, coord in zip(coord_motor_list, initial_coords):
                yield from mv(motor, coord)
            if shutter is not None:
                yield from mv(shutter, 1)
            yield from close_run()

    def _cleanup():
        for key, val in saved.items():
            if isinstance(key, tuple) and len(key) == 3 and key[0] == '_stage_sigs':
                _, obj, attr = key
                obj.stage_sigs[attr] = val
            else:
                yield from bps.mv(key, val)

    yield from bpp.finalize_wrapper(_body(), _cleanup())


# ---------------------------------------------------------------------------
# Flow control
# ---------------------------------------------------------------------------

def switch_flow(fluigent_channel: int, flow_rate: float, md: dict = None):
    """Switch Fluigent channel flow rate.

    Parameters
    ----------
    fluigent_channel : int
        Channel index (0 or 1).
    flow_rate : float
        Target flow rate for the selected channel.
    md : dict, optional
        Unused — accepted for queue-server compatibility.
    """
    opposite_channel = 1 - fluigent_channel
    PressureSet          = EpicsSignal(f"15ID:Fluigent:EZ:{fluigent_channel}:PressureSet")
    FlowRateSet          = EpicsSignal(f"15ID:Fluigent:EZ:{fluigent_channel}:FlowRateSet")
    ValveSet             = EpicsSignal(f"15ID:Fluigent:EZ:{fluigent_channel}:ValveSet")
    PressureSet_opposite = EpicsSignal(f"15ID:Fluigent:EZ:{opposite_channel}:PressureSet")
    FlowRateSet_opposite = EpicsSignal(f"15ID:Fluigent:EZ:{opposite_channel}:FlowRateSet")
    ValveSet_opposite    = EpicsSignal(f"15ID:Fluigent:EZ:{opposite_channel}:ValveSet")

    yield from bps.mv(ValveSet_opposite, 0)
    yield from bps.mv(PressureSet_opposite, 0)
    yield from bps.mv(ValveSet, 1)
    yield from bps.mv(FlowRateSet, flow_rate)


def wait_for_eq(time: float, md: dict = None):
    """Wait for equilibration.

    Parameters
    ----------
    time : float
        Wait duration in seconds.
    md : dict, optional
        Unused — accepted for queue-server compatibility.
    """
    yield from sleep(time)


# ---------------------------------------------------------------------------
# Capillary alignment
# ---------------------------------------------------------------------------

def smooth_1d(y, sigma=1):
    y = np.asarray(y, dtype=float)
    if sigma is None or sigma <= 0:
        return y
    radius = int(4 * sigma + 0.5)
    x = np.arange(-radius, radius + 1)
    kernel = np.exp(-(x ** 2) / (2 * sigma ** 2))
    kernel = kernel / kernel.sum()
    return np.convolve(y, kernel, mode="same")


def extract_capillary_centers_from_arrays(
    x, ch2, ch8,
    smooth_sigma=1,
    threshold_fraction=0.55,
    min_points=3,
):
    """Extract capillary center positions from normalised transmission dips."""
    x   = np.asarray(x,   dtype=float)
    ch2 = np.asarray(ch2, dtype=float)
    ch8 = np.asarray(ch8, dtype=float)

    good = np.isfinite(x) & np.isfinite(ch2) & np.isfinite(ch8) & (ch2 != 0)
    x, ch2, ch8 = x[good], ch2[good], ch8[good]
    y_norm = ch8 / ch2

    order  = np.argsort(x)
    x, y_norm = x[order], y_norm[order]
    y_smooth  = smooth_1d(y_norm, sigma=smooth_sigma)

    high      = np.percentile(y_smooth, 85)
    low       = np.percentile(y_smooth, 10)
    threshold = low + threshold_fraction * (high - low)
    mask      = y_smooth < threshold

    runs, start = [], None
    for i, is_dip in enumerate(mask):
        if is_dip and start is None:
            start = i
        if (not is_dip or i == len(mask) - 1) and start is not None:
            end = i - 1 if not is_dip else i
            if end - start + 1 >= min_points:
                runs.append((start, end))
            start = None

    centers = []
    for s, e in runs:
        if s == 0 or e == len(x) - 1:
            centers.append(float(x[s:e + 1][np.argmin(y_smooth[s:e + 1])]))
            continue

        def interp_cross(i1, i2):
            x1, x2 = x[i1], x[i2]
            y1, y2 = y_smooth[i1], y_smooth[i2]
            return 0.5 * (x1 + x2) if y2 == y1 else x1 + (threshold - y1) * (x2 - x1) / (y2 - y1)

        centers.append(0.5 * (interp_cross(s - 1, s) + interp_cross(e, e + 1)))

    return centers, y_norm, y_smooth, threshold


def sort_centers_by_scan_direction(centers_x, x_range):
    x_start, x_stop = x_range
    return sorted(centers_x, reverse=(x_start > x_stop))


def capillary_transmission_scan_plan(
    detectors,
    sx,
    sy,
    x_range,
    num_points: int,
    y_fixed: float = -28.40000,
    exposure_time: float = 1.0,
    ch2_key: str = "2",
    ch8_key: str = "8",
    smooth_sigma: float = 1.0,
    threshold_fraction: float = 0.55,
    min_points: int = 3,
    shutter: Optional[Device] = None,
    hdf_autosave: bool = True,
    md: dict = None,
):
    """Scan across capillaries, extract their centres, and save to CSV.

    Parameters
    ----------
    detectors :
        Detectors to read (must include channels ch2_key and ch8_key).
    sx :
        Horizontal scan motor.
    sy :
        Vertical motor (moved once to y_fixed before the scan).
    x_range :
        [x_start, x_stop] — scan range.  Direction sets sort order.
    num_points : int
        Number of x positions in the scan.
    y_fixed : float
        Fixed y position during the scan.
    exposure_time : float
        Detector exposure time in seconds.
    ch2_key : str
        Reading key for the I0 (incident beam) channel.
    ch8_key : str
        Reading key for the transmission channel.
    smooth_sigma : float
        Gaussian smoothing sigma for centre extraction.
    threshold_fraction : float
        Threshold fraction (0–1) for dip detection.
    min_points : int
        Minimum dip width in points.
    shutter : Device, optional
        Fast shutter device.
    hdf_autosave : bool
        True (default) saves HDF files; False disables auto_save.
    md : dict, optional
        Additional metadata (experiment fields auto-injected by EasyBluesky).
    """
    md = md or {}
    shutter = _resolve_device(shutter)
    _dir = _exp_dir_from_md(md)

    scan_md = dict(md)
    scan_md.update({
        "plan_name":      "capillary_transmission_scan_plan",
        "x_range":        list(x_range),
        "fixed_y":        y_fixed,
        "normalization":  f"{ch8_key} / {ch2_key}",
        "center_sorting": "large_to_small" if x_range[0] > x_range[1] else "small_to_large",
    })

    x_data, ch2_data, ch8_data = [], [], []

    @bpp.run_decorator(md=scan_md)
    def inner_scan():
        for x in np.linspace(x_range[0], x_range[1], num_points):
            yield from bps.mv(sx, float(x))
            if shutter is not None:
                yield from mv(shutter, 0)
            yield from sleep(0.3)
            reading = yield from bps.trigger_and_read(detectors + [sx, sy])
            if shutter is not None:
                yield from mv(shutter, 1)
            x_data.append(float(reading[sx.name]["value"]))
            ch2_data.append(float(reading[ch2_key]["value"]))
            ch8_data.append(float(reading[ch8_key]["value"]))

    saved = {}

    def _body():
        yield from _save_and_set_det_mode(detectors, hdf_autosave, saved)
        for detector in detectors:
            yield from set_detector_acquire_time(detector, exposure_time)
        yield from bps.mv(sy, y_fixed)
        yield from inner_scan()

        centers_x, y_norm, y_smooth, threshold = extract_capillary_centers_from_arrays(
            x=x_data, ch2=ch2_data, ch8=ch8_data,
            smooth_sigma=smooth_sigma,
            threshold_fraction=threshold_fraction,
            min_points=min_points,
        )
        centers_x  = sort_centers_by_scan_direction(centers_x, x_range=x_range)
        centers_xy = [(float(x), float(y_fixed)) for x in centers_x]

        print("=" * 80)
        print("Extracted capillary centers:")
        print(f"Normalization: {ch8_key} / {ch2_key}")
        print(f"x_range: {x_range}")
        print(f"Sorting: {'large to small' if x_range[0] > x_range[1] else 'small to large'}")
        print()
        print(", ".join(f"({x:.5f}, {y:.5f})" for x, y in centers_xy))
        print("=" * 80)

        save_path = os.path.join(_dir, "sample_positions.csv")
        with open(save_path, "w") as f:
            f.write(", ".join(f"({x:.5f}, {y:.5f})" for x, y in centers_xy))
        print(f"Saved centre list to: {save_path}")

        return centers_xy

    def _cleanup():
        for key, val in saved.items():
            if isinstance(key, tuple) and len(key) == 3 and key[0] == '_stage_sigs':
                _, obj, attr = key
                obj.stage_sigs[attr] = val
            else:
                yield from bps.mv(key, val)

    return (yield from bpp.finalize_wrapper(_body(), _cleanup()))
