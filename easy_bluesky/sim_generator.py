"""sim_generator.py — Parse a real startup script and produce a simulated equivalent."""

import ast
from pathlib import Path

# ── Device-type classification ─────────────────────────────────────────────────

_MOTOR_HINTS = {
    'epicsmotor', 'motor', 'positioner', 'newport', 'twincat',
    'softpositioner', 'pseudopositioner', 'flyer',
}
_AD_HINTS = {
    'areadetector', 'singletrigger', 'pilatus', 'eiger', 'pointgrey',
    'prosilica', 'perkin', 'andor', 'simdetector', 'psl', 'dectris',
    'xspress', 'detector', 'cam',
}
_SCALAR_HINTS = {'scaler', 'counter', 'diode', 'ionc'}

_SKIP_CLASSES = {
    'RunEngine', 'RunRouter', 'Context', 'Path', 'dict', 'list',
    'set', 'tuple', 'int', 'str', 'float', 'bool', 'Queue',
    'Thread', 'Event', 'Lock', 'Timer', 'Socket',
}


def _classify(class_name: str) -> str:
    low = class_name.lower()
    for h in _AD_HINTS:
        if h in low:
            return 'area_det'
    for h in _MOTOR_HINTS:
        if h in low:
            return 'motor'
    for h in _SCALAR_HINTS:
        if h in low:
            return 'scalar_det'
    return 'unknown'


def _name_kwarg(call_node) -> str | None:
    for kw in call_node.keywords:
        if kw.arg == 'name' and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def parse_devices(script_path: str | Path) -> list[dict]:
    """
    Return list of dicts: {'var', 'name', 'class', 'kind'}
    kind ∈ {'motor', 'area_det', 'scalar_det', 'unknown_device'}
    """
    source = Path(script_path).read_text()
    tree   = ast.parse(source)

    # Collect class defs that inherit from AD-related bases
    ad_class_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                bname = (base.id if isinstance(base, ast.Name) else
                         base.attr if isinstance(base, ast.Attribute) else '')
                if _classify(bname) == 'area_det':
                    ad_class_names.add(node.name)

    devices = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        var = node.targets[0].id
        if not isinstance(node.value, ast.Call):
            continue

        func = node.value.func
        cname = (func.id if isinstance(func, ast.Name) else
                 func.attr if isinstance(func, ast.Attribute) else None)
        if cname is None or cname in _SKIP_CLASSES:
            continue

        dev_name = _name_kwarg(node.value) or var

        if cname in ad_class_names:
            kind = 'area_det'
        else:
            kind = _classify(cname)
            if kind == 'unknown':
                if _name_kwarg(node.value) is None:
                    continue          # no name= kwarg → not a bluesky device
                kind = 'unknown_device'

        devices.append({'var': var, 'name': dev_name, 'class': cname, 'kind': kind})

    return devices


# ── SimAreaDetector source block ───────────────────────────────────────────────

_SIM_AD_SRC = '''\
import numpy as _np
import time as _time
from ophyd import Device, Component as Cpt, Signal, DeviceStatus


class _SimCam(Device):
    acquire_time   = Cpt(Signal, value=1.0,        kind='config')
    acquire_period = Cpt(Signal, value=1.1,        kind='config')
    num_images     = Cpt(Signal, value=1,          kind='config')
    image_mode     = Cpt(Signal, value=0,          kind='config')
    gain           = Cpt(Signal, value=1.0,        kind='config')
    trigger_mode   = Cpt(Signal, value=0,          kind='config')


class _SimHDF5Plugin(Device):
    file_path      = Cpt(Signal, value='/tmp/',    kind='config')
    file_name      = Cpt(Signal, value='sim',      kind='config')
    file_number    = Cpt(Signal, value=1,          kind='config')
    num_capture    = Cpt(Signal, value=1,          kind='config')
    enable         = Cpt(Signal, value=1,          kind='config')
    auto_save      = Cpt(Signal, value=1,          kind='config')
    auto_increment = Cpt(Signal, value=1,          kind='config')
    file_template  = Cpt(Signal, value='%s%s_%d.h5', kind='config')


class _SimImagePlugin(Device):
    array_data  = Cpt(Signal, value=0,   kind='normal')
    array_size0 = Cpt(Signal, value=512, kind='config')
    array_size1 = Cpt(Signal, value=512, kind='config')


class SimAreaDetector(Device):
    """
    Simulated area detector mirroring the ophyd SingleTrigger+AreaDetector
    interface.  cam / hdf1 / image sub-devices are all settable via bps.mv /
    bps.abs_set.  trigger() returns an immediately-completed Status.
    read() returns total_counts and mean_intensity as scalar values.
    """
    cam   = Cpt(_SimCam,          '', kind='config')
    hdf1  = Cpt(_SimHDF5Plugin,   '', kind='config')
    image = Cpt(_SimImagePlugin,  '', kind='normal')
    total_counts   = Cpt(Signal, value=0,   kind='hinted')
    mean_intensity = Cpt(Signal, value=0.0, kind='normal')

    def __init__(self, *args, shape=(512, 512), background=100.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._shape      = shape
        self._background = background

    def trigger(self):
        img = _np.random.poisson(self._background, self._shape).astype(_np.float32)
        self.total_counts.put(int(img.sum()))
        self.mean_intensity.put(float(img.mean()))
        self.image.array_data.put(int(img.sum()))
        st = DeviceStatus(self)
        st._finished()
        return st

    def stage(self):
        return super().stage()

    def unstage(self):
        return super().unstage()
'''


def generate_sim_script(real_script_path: str | Path,
                        output_path: str | Path | None = None) -> Path:
    """
    Parse *real_script_path* and write a simulated equivalent to *output_path*
    (default: same directory, file name ``re_startup_sim.py``).
    Returns the output path.
    """
    real_path = Path(real_script_path)
    if output_path is None:
        output_path = real_path.parent / 're_startup_sim.py'
    output_path = Path(output_path)

    devices = parse_devices(real_path)

    lines: list[str] = [
        '"""',
        're_startup_sim.py — AUTO-GENERATED simulated startup script.',
        f'Generated from: {real_path.name}',
        '',
        'Edit freely.  Re-generate from File → Generate Sim Script to pick up',
        'new real-hardware devices added to the original startup script.',
        '"""',
        '',
        '# ── Run Engine ────────────────────────────────────────────────────────────',
        'from bluesky import RunEngine',
        'RE = RunEngine({})',
        '',
        '# ── SimAreaDetector ────────────────────────────────────────────────────────',
        _SIM_AD_SRC.rstrip(),
        '',
        '',
        '# ── Simulated devices (auto-mapped from real script) ───────────────────────',
        'from ophyd.sim import SynAxis, SynGauss',
        '',
    ]

    motor_vars: list[str] = []
    for d in devices:
        var, name, kind = d['var'], d['name'], d['kind']
        if kind == 'motor':
            lines.append(f"{var} = SynAxis(name='{name}')")
            motor_vars.append(var)
        elif kind == 'area_det':
            lines.append(f"{var} = SimAreaDetector(name='{name}')")
        elif kind in ('scalar_det', 'unknown_device'):
            ref = motor_vars[0] if motor_vars else None
            if ref:
                lines.append(
                    f"{var} = SynGauss('{name}', {ref}, '{ref}', "
                    f"center=0, Imax=1000, sigma=0.5, noise='poisson')"
                )
            else:
                lines.append(
                    f"{var} = SynGauss('{name}', motor1, 'motor1', "
                    f"center=0, Imax=1000, sigma=0.5, noise='poisson')"
                )

    if not devices:
        lines += [
            '# No devices detected — adding generic defaults',
            "motor1 = SynAxis(name='motor1')",
            "motor2 = SynAxis(name='motor2')",
            "det    = SynGauss('det', motor1, 'motor1', center=0, Imax=1000, sigma=0.5)",
            "sim_ad = SimAreaDetector(name='sim_ad')",
        ]

    lines += [
        '',
        '# ── Standard bluesky plans ─────────────────────────────────────────────────',
        'from bluesky.plans import (',
        '    count, scan, rel_scan, grid_scan, rel_grid_scan,',
        '    adaptive_scan, spiral, spiral_fermat,',
        ')',
        'from bluesky.plan_stubs import mv, mvr, sleep, rd',
        '',
    ]

    # ── Suitcase + ZMQ block (fixed, not copied from real script) ────────────────
    # Uses sim ZMQ port 60631 by default (override with BLUESKY_ZMQ_PUB_PORT).
    lines += [
        '# ── Data routing (suitcase.jsonl) ─────────────────────────────────────────',
        'import os',
        'from pathlib import Path',
        '',
        '_script_dir = Path(__file__).parent',
        '_default_data_dir = _script_dir.parent / "data" / "runs"',
        '_DATA_DIR = Path(os.getenv("BLUESKY_DATA_DIR", str(_default_data_dir)))',
        '_DATA_DIR.mkdir(parents=True, exist_ok=True)',
        '_ACTIVE_EXP_FILE = _script_dir.parent / "data" / "active_experiment.json"',
        '',
        'try:',
        '    import suitcase.jsonl',
        '    from event_model import RunRouter',
        '',
        '    def _jsonl_factory(name, doc):',
        '        runs_dir = _DATA_DIR',
        '        try:',
        '            if _ACTIVE_EXP_FILE.exists():',
        '                import json as _j',
        '                info = _j.loads(_ACTIVE_EXP_FILE.read_text())',
        '                candidate = Path(info["path"]) / "runs"',
        '                candidate.mkdir(parents=True, exist_ok=True)',
        '                runs_dir = candidate',
        '        except Exception:',
        '            pass',
        '        return [suitcase.jsonl.Serializer(str(runs_dir))], []',
        '',
        '    RE.subscribe(RunRouter([_jsonl_factory]))',
        'except Exception as e:',
        '    print(f"[re_startup_sim] WARNING: suitcase.jsonl not subscribed: {e}")',
        '',
        '# ── ZMQ PUB for Live Viewer (sim port 60631) ───────────────────────────────',
        '_ZMQ_PUB_PORT = int(os.getenv("BLUESKY_ZMQ_PUB_PORT", "60631"))',
        'try:',
        '    import zmq as _zmq',
        '    import json as _json',
        '    from event_model import sanitize_doc as _sanitize',
        '',
        '    _zmq_ctx  = _zmq.Context()',
        '    _zmq_sock = _zmq_ctx.socket(_zmq.PUB)',
        '    _zmq_sock.bind(f"tcp://*:{_ZMQ_PUB_PORT}")',
        '',
        '    def _zmq_publish(name, doc):',
        '        try:',
        '            _zmq_sock.send_string(_json.dumps([name, dict(_sanitize(doc))]))',
        '        except Exception:',
        '            pass',
        '',
        '    RE.subscribe(_zmq_publish)',
        '    print(f"[re_startup_sim] ZMQ PUB → tcp://*:{_ZMQ_PUB_PORT}")',
        'except Exception as e:',
        '    print(f"[re_startup_sim] WARNING: ZMQ PUB not started: {e}")',
        '',
        'print("[re_startup_sim] Simulated RE environment ready")',
    ]

    output_path.write_text('\n'.join(lines) + '\n')
    return output_path
