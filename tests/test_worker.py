"""
tests/test_worker.py
--------------------
Basic tests for the ZMQ worker and config modules.
Run with: pytest tests/
"""

import pytest
from unittest.mock import MagicMock, patch


def test_config_defaults():
    from bluesky_app.config import ZMQ_CONTROL, ZMQ_INFO, KAFKA_SERVER
    assert ZMQ_CONTROL.startswith("tcp://")
    assert ZMQ_INFO.startswith("tcp://")
    assert ":" in KAFKA_SERVER


def test_config_env_override(monkeypatch):
    monkeypatch.setenv("BLUESKY_ZMQ_CONTROL", "tcp://10.0.0.1:60615")
    import importlib
    import bluesky_app.config as cfg
    importlib.reload(cfg)
    assert cfg.ZMQ_CONTROL == "tcp://10.0.0.1:60615"


def test_plot_colors_defined():
    from bluesky_app.config import PLOT_COLORS
    assert len(PLOT_COLORS) >= 4
    for c in PLOT_COLORS:
        assert c.startswith("#")


@pytest.mark.skipif(
    True, reason="Requires running RE Manager — integration test only"
)
def test_zmq_worker_connect():
    from bluesky_app.worker import ZMQWorker
    w = ZMQWorker()
    ok = w.connect()
    assert ok
    assert w.rm is not None


def test_paramform_numeric(qtbot):
    """ParamForm generates a QDoubleSpinBox for float params."""
    from bluesky_app.widgets import ParamForm
    params = [
        {
            "name": "start",
            "kind": {"name": "POSITIONAL_OR_KEYWORD", "value": 1},
            "annotation": {"type": "float"},
            "description": "start position",
        }
    ]
    form = ParamForm(params, {})
    qtbot.addWidget(form)
    from PyQt6.QtWidgets import QDoubleSpinBox
    assert "start" in form.widgets
    assert isinstance(form.widgets["start"], QDoubleSpinBox)


def test_paramform_device_list(qtbot):
    """ParamForm generates a QListWidget for READABLE device params."""
    from bluesky_app.widgets import ParamForm
    params = [
        {
            "name": "detectors",
            "kind": {"name": "POSITIONAL_OR_KEYWORD", "value": 1},
            "annotation": {"type": "collections.abc.Sequence[__READABLE__]"},
            "description": "detectors",
            "convert_device_names": True,
        }
    ]
    devices = {"det1": {}, "det2": {}}
    form = ParamForm(params, devices)
    qtbot.addWidget(form)
    from PyQt6.QtWidgets import QListWidget
    assert "detectors" in form.widgets
    assert isinstance(form.widgets["detectors"], QListWidget)
    assert form.widgets["detectors"].count() == 2
