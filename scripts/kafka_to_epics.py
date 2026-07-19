#!/usr/bin/env python3
"""
kafka_to_epics.py
-----------------
Reads bluesky event documents from Kafka and publishes
data values to soft EPICS PVs via caproto.

Publishes both scalar PVs (latest value) and array PVs
(accumulated per scan) for XY plotting in Phoebus.

Usage:
    python3 kafka_to_epics.py

Then in Phoebus Display Builder, add an XY Plot widget with:
    X PV: BLUESKY:motor1_array
    Y PV: BLUESKY:det1_array

Requirements:
    pip install caproto confluent-kafka msgpack msgpack-numpy
"""

import os
import threading
import msgpack
import msgpack_numpy as mpn
from confluent_kafka import Consumer
from caproto.server import pvproperty, PVGroup, ioc_arg_parser, run

# ── Configuration ──────────────────────────────────────────────────────────────
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC             = os.getenv("KAFKA_TOPIC", "bluesky.runengine.documents")
PV_PREFIX               = os.getenv("BLUESKY_PV_PREFIX", "BLUESKY:")
MAX_POINTS              = 10000  # max array size

# ── Shared state ───────────────────────────────────────────────────────────────
_scalar_values = {}          # {signal_name: latest_value}
_array_values  = {}          # {signal_name: [v1, v2, ...]}
_num_points    = 0           # number of points collected in current scan
_lock          = threading.Lock()

# ── Kafka consumer thread ──────────────────────────────────────────────────────
def kafka_consumer_thread():
    global _num_points

    c = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
        "group.id":          "bluesky-epics-bridge",
        "auto.offset.reset": "latest",
    })
    c.subscribe([KAFKA_TOPIC])
    print(f"[Kafka] Subscribed to {KAFKA_TOPIC} on {KAFKA_BOOTSTRAP_SERVERS}")

    while True:
        msg = c.poll(1.0)
        if msg is None:
            continue
        if msg.error():
            print(f"[Kafka] Error: {msg.error()}")
            continue

        try:
            doc = msgpack.unpackb(msg.value(), object_hook=mpn.decode)
            if not isinstance(doc, list) or len(doc) < 2:
                continue

            name, body = doc[0], doc[1]

            if name == "start":
                # Reset arrays for new scan
                with _lock:
                    _array_values.clear()
                    _num_points = 0
                print(f"[Kafka] New run: {body.get('plan_name','unknown')} "
                      f"uid={body.get('uid','')[:8]}")

            elif name == "event":
                data = body.get("data", {})
                seq  = body.get("seq_num", "?")
                print(f"[Kafka] Event #{seq}: { {k: round(v,4) if isinstance(v,float) else v for k,v in data.items()} }")
                with _lock:
                    # Update scalar values
                    _scalar_values.update(data)
                    _scalar_values["seq_num"] = seq
                    # Accumulate array values
                    for k, v in data.items():
                        try:
                            fv = float(v)
                        except (TypeError, ValueError):
                            continue
                        if k not in _array_values:
                            _array_values[k] = []
                        _array_values[k].append(fv)
                    _num_points = seq

            elif name == "stop":
                print(f"[Kafka] Run stopped: {body.get('exit_status','unknown')} "
                      f"({_num_points} points collected)")

        except Exception as e:
            print(f"[Kafka] Decode error: {e}")


# ── Helpers ────────────────────────────────────────────────────────────────────
def get_scalar(key, default=0.0):
    with _lock:
        return _scalar_values.get(key, default)

def get_array(key, size=MAX_POINTS, default=0.0):
    with _lock:
        arr = list(_array_values.get(key, []))
    # Pad to fixed size so PV length stays constant
    if len(arr) < size:
        arr = arr + [default] * (size - len(arr))
    return arr[:size]

def get_num_points():
    with _lock:
        return _num_points


# ── EPICS IOC ──────────────────────────────────────────────────────────────────
class BlueskyIOC(PVGroup):

    # ── Scalar PVs (latest value) ──────────────────────────────────────────────
    motor   = pvproperty(name="motor",   value=0.0,  dtype=float)
    motor1  = pvproperty(name="motor1",  value=0.0,  dtype=float)
    motor2  = pvproperty(name="motor2",  value=0.0,  dtype=float)
    det     = pvproperty(name="det",     value=0.0,  dtype=float)
    det1    = pvproperty(name="det1",    value=0.0,  dtype=float)
    det2    = pvproperty(name="det2",    value=0.0,  dtype=float)
    det3    = pvproperty(name="det3",    value=0.0,  dtype=float)
    seq_num = pvproperty(name="seq_num", value=0,    dtype=int)
    num_points = pvproperty(name="num_points", value=0, dtype=int)

    # ── Array PVs (accumulated per scan, for XY plotting) ─────────────────────
    motor_array   = pvproperty(name="motor_array",   value=[0.0]*MAX_POINTS, dtype=float)
    motor1_array  = pvproperty(name="motor1_array",  value=[0.0]*MAX_POINTS, dtype=float)
    motor2_array  = pvproperty(name="motor2_array",  value=[0.0]*MAX_POINTS, dtype=float)
    det_array     = pvproperty(name="det_array",     value=[0.0]*MAX_POINTS, dtype=float)
    det1_array    = pvproperty(name="det1_array",    value=[0.0]*MAX_POINTS, dtype=float)
    det2_array    = pvproperty(name="det2_array",    value=[0.0]*MAX_POINTS, dtype=float)
    det3_array    = pvproperty(name="det3_array",    value=[0.0]*MAX_POINTS, dtype=float)

    # ── Scalar scan handlers ───────────────────────────────────────────────────
    @motor.scan(period=0.5)
    async def motor(self, instance, async_lib):
        await instance.write(get_scalar("motor"))

    @motor1.scan(period=0.5)
    async def motor1(self, instance, async_lib):
        await instance.write(get_scalar("motor1"))

    @motor2.scan(period=0.5)
    async def motor2(self, instance, async_lib):
        await instance.write(get_scalar("motor2"))

    @det.scan(period=0.5)
    async def det(self, instance, async_lib):
        await instance.write(get_scalar("det"))

    @det1.scan(period=0.5)
    async def det1(self, instance, async_lib):
        await instance.write(get_scalar("det1"))

    @det2.scan(period=0.5)
    async def det2(self, instance, async_lib):
        await instance.write(get_scalar("det2"))

    @det3.scan(period=0.5)
    async def det3(self, instance, async_lib):
        await instance.write(get_scalar("det3"))

    @seq_num.scan(period=0.5)
    async def seq_num(self, instance, async_lib):
        await instance.write(int(get_scalar("seq_num", 0)))

    @num_points.scan(period=0.5)
    async def num_points(self, instance, async_lib):
        await instance.write(get_num_points())

    # ── Array scan handlers ────────────────────────────────────────────────────
    @motor_array.scan(period=0.5)
    async def motor_array(self, instance, async_lib):
        await instance.write(get_array("motor"))

    @motor1_array.scan(period=0.5)
    async def motor1_array(self, instance, async_lib):
        await instance.write(get_array("motor1"))

    @motor2_array.scan(period=0.5)
    async def motor2_array(self, instance, async_lib):
        await instance.write(get_array("motor2"))

    @det_array.scan(period=0.5)
    async def det_array(self, instance, async_lib):
        await instance.write(get_array("det"))

    @det1_array.scan(period=0.5)
    async def det1_array(self, instance, async_lib):
        await instance.write(get_array("det1"))

    @det2_array.scan(period=0.5)
    async def det2_array(self, instance, async_lib):
        await instance.write(get_array("det2"))

    @det3_array.scan(period=0.5)
    async def det3_array(self, instance, async_lib):
        await instance.write(get_array("det3"))


def main():
    print("=" * 60)
    print("Bluesky Kafka → EPICS Bridge")
    print("=" * 60)
    print(f"PV Prefix : {PV_PREFIX}")
    print(f"Max Points: {MAX_POINTS}")
    print()
    print("Scalar PVs (latest value):")
    for sig in ["motor1", "motor2", "det1", "det2", "det", "seq_num", "num_points"]:
        print(f"  {PV_PREFIX}{sig}")
    print()
    print("Array PVs (for XY plotting in Phoebus Display Builder):")
    for sig in ["motor1_array", "motor2_array", "det1_array", "det2_array", "det_array"]:
        print(f"  {PV_PREFIX}{sig}")
    print()
    print("In Phoebus Display Builder, add an XY Plot widget:")
    print(f"  X PV: {PV_PREFIX}motor1_array")
    print(f"  Y PV: {PV_PREFIX}det1_array")
    print("=" * 60)

    # Start Kafka consumer thread
    t = threading.Thread(target=kafka_consumer_thread, daemon=True)
    t.start()

    # Start caproto EPICS IOC
    ioc_options, run_options = ioc_arg_parser(
        default_prefix=PV_PREFIX,
        desc="Bluesky Kafka to EPICS bridge for live XY plotting"
    )
    ioc = BlueskyIOC(**ioc_options)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()
