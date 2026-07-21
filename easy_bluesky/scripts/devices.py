"""
devices.py — Hardware device definitions.

Edit this file to add your ophyd/EPICS devices. It is imported
automatically by re_startup_mongo.py — you do not need to touch
the startup script.

The scripts directory is on sys.path, so you can also split
devices across multiple files and import them here.

Example:
    from ophyd import EpicsMotor, EpicsSignal, Component as Cpt

    m1 = EpicsMotor("IOC:m1", name="m1")
    m2 = EpicsMotor("IOC:m2", name="m2")
    det = EpicsSignal("IOC:det", name="det")
"""

from ophyd import EpicsMotor

# Add your devices below:
# m1 = EpicsMotor("IOC:m1", name="m1")
