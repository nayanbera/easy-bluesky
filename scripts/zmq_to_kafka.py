#!/usr/bin/env python3
"""
zmq_to_kafka.py
---------------
Bridges Bluesky RE Manager ZMQ stream to Kafka.
Subscribes to RE Manager's ZMQ info socket and forwards
bluesky documents to a Kafka topic for live plotting.

Usage:
    python3 zmq_to_kafka.py

Requirements:
    pip install bluesky-queueserver-api bluesky-kafka msgpack

Configuration via environment variables:
    QSERVER_ZMQ_INFO_ADDRESS   - ZMQ info address (default: tcp://localhost:60625)
    KAFKA_BOOTSTRAP_SERVERS    - Kafka server (default: localhost:9092)
    KAFKA_TOPIC                - Kafka topic (default: bluesky.runengine.documents)
"""

import os
import time
import msgpack
import msgpack_numpy as mpn
import zmq
from confluent_kafka import Producer

# Configuration
ZMQ_INFO_ADDR = os.getenv("QSERVER_ZMQ_INFO_ADDRESS", "tcp://localhost:60625")
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC = os.getenv("KAFKA_TOPIC", "bluesky.runengine.documents")

print(f"ZMQ Info Address      : {ZMQ_INFO_ADDR}")
print(f"Kafka Bootstrap Server: {KAFKA_BOOTSTRAP_SERVERS}")
print(f"Kafka Topic           : {KAFKA_TOPIC}")
print("-" * 50)


def delivery_report(err, msg):
    """Kafka delivery callback."""
    if err is not None:
        print(f"[Kafka] Delivery failed: {err}")
    else:
        print(f"[Kafka] Delivered to {msg.topic()} [{msg.partition()}] offset {msg.offset()}")


def serialize(doc):
    """Serialize a bluesky document to msgpack bytes."""
    return msgpack.packb(doc, default=mpn.encode)


def main():
    # Set up Kafka producer
    producer = Producer({
        "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
        "enable.idempotence": True,
    })
    print(f"[Kafka] Producer connected to {KAFKA_BOOTSTRAP_SERVERS}")

    # Set up ZMQ subscriber
    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    socket.connect(ZMQ_INFO_ADDR)
    socket.setsockopt(zmq.SUBSCRIBE, b"")  # Subscribe to all topics
    print(f"[ZMQ] Subscribed to {ZMQ_INFO_ADDR}")
    print("Waiting for bluesky documents...\n")

    try:
        while True:
            try:
                # Non-blocking receive with timeout
                if socket.poll(timeout=1000):  # 1 second timeout
                    raw = socket.recv_multipart()

                    # RE Manager publishes [topic, payload]
                    if len(raw) >= 2:
                        topic = raw[0].decode("utf-8", errors="ignore")
                        payload = raw[1]

                        try:
                            # Try to decode as msgpack
                            doc = msgpack.unpackb(payload, raw=False, object_hook=mpn.decode)
                        except Exception:
                            try:
                                # Try as plain string/json
                                import json
                                doc = json.loads(payload.decode("utf-8"))
                            except Exception as e:
                                print(f"[Warning] Could not decode message: {e}")
                                continue

                        # Filter for bluesky document topics
                        if any(t in topic for t in ["start", "event", "stop", "descriptor", "resource", "datum"]):
                            print(f"[ZMQ->Kafka] Topic: {topic}, Doc type: {doc.get('name', 'unknown') if isinstance(doc, dict) else 'raw'}")

                            # Forward to Kafka
                            producer.produce(
                                KAFKA_TOPIC,
                                key=topic.encode("utf-8"),
                                value=serialize(doc),
                                callback=delivery_report
                            )
                            producer.poll(0)  # Trigger delivery callbacks

                        else:
                            # Still print other messages for debugging
                            print(f"[ZMQ] Non-document topic: {topic}")

            except zmq.ZMQError as e:
                print(f"[ZMQ Error] {e}")
                time.sleep(1)

            # Flush Kafka periodically
            producer.flush(timeout=0.1)

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        producer.flush(timeout=5)
        socket.close()
        context.term()
        print("Done.")


if __name__ == "__main__":
    main()
