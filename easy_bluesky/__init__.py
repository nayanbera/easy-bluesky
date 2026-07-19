"""
EasyBluesky
-------------------
A PyQt6 desktop application for controlling and monitoring
Bluesky experiments via the queue server (ZMQ transport).

Modules:
    config      — configuration constants
    worker      — ZMQ worker thread
    highlighter — Python syntax highlighter
    widgets     — reusable Qt widgets (ParamForm, PlanDialog)
    queue_mgr   — Queue Manager tab
    plan_builder — Plan Builder tab
    live_viewer  — Live Viewer tab (Kafka + pyqtgraph)
    data_browser — Data Browser tab (Databroker)
    main        — MainWindow and entry point
"""

__version__ = "0.1.0"
__author__  = "Your Name"
