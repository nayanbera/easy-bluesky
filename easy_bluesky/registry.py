"""registry.py — Shared registry of RE Manager instances across the network."""

import concurrent.futures
import hashlib
import json
import secrets
import socket


REGISTRY_DEFAULTS = {
    "version": 1,
    "admin_password_hash": "",
    "instances": [],
}

INSTANCE_DEFAULTS = {
    "name": "",
    "host": "",
    "description": "",
    "control_port": 60615,
    "info_port": 60625,
    "doc_port": 60630,
    "procserv_port": 60635,
    "devices_file": "devices.py",
    "conda_env":    "",
    "conda_path":   "~/miniconda3",
}


# ── password hashing ───────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Return a pbkdf2:sha256:SALT:HASH string suitable for storage."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"pbkdf2:sha256:{salt}:{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Verify password against a stored pbkdf2 hash string."""
    try:
        _, algo, salt, stored_hex = stored.split(":")
        if algo != "sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
        return dk.hex() == stored_hex
    except Exception:
        return False


# ── SSH registry I/O ───────────────────────────────────────────────────────────

def _registry_ssh_settings(settings: dict) -> dict:
    """Return settings dict with host set to the registry host."""
    host = settings.get("registry_host", "").strip() or settings.get("host", "")
    s = dict(settings)
    s["host"] = host
    return s


def fetch_registry(settings: dict) -> dict:
    """SSH to the registry host and return the parsed registry dict.

    Returns REGISTRY_DEFAULTS on any error so callers can fall back gracefully.
    """
    from .ssh_manager import _get_client
    reg = _registry_ssh_settings(settings)
    if not reg.get("host"):
        return dict(REGISTRY_DEFAULTS)
    try:
        client = _get_client(reg)
        _, stdout, _ = client.exec_command(
            "cat ~/.easy_bluesky/registry.json 2>/dev/null", timeout=8
        )
        raw = stdout.read().decode().strip()
        client.close()
        if not raw:
            return dict(REGISTRY_DEFAULTS)
        data = json.loads(raw)
        data.setdefault("version", 1)
        data.setdefault("admin_password_hash", "")
        data.setdefault("instances", [])
        return data
    except Exception:
        return dict(REGISTRY_DEFAULTS)


def save_registry(settings: dict, registry: dict):
    """SSH to the registry host and write the registry as JSON.

    Raises on SSH or write failure.
    """
    from .ssh_manager import _get_client
    reg = _registry_ssh_settings(settings)
    if not reg.get("host"):
        raise ValueError("No registry host configured")
    client = _get_client(reg)
    try:
        client.exec_command("mkdir -p ~/.easy_bluesky", timeout=5)
        _, out, _ = client.exec_command("echo $HOME", timeout=5)
        home = out.read().decode().strip() or "/tmp"
        sftp = client.open_sftp()
        path = f"{home}/.easy_bluesky/registry.json"
        with sftp.open(path, "w") as f:
            f.write(json.dumps(registry, indent=2))
        sftp.close()
    finally:
        client.close()


# ── running-status probe ───────────────────────────────────────────────────────

def probe_instance_running(host: str, port: int, timeout: float = 2.0) -> bool:
    """Return True if something is listening on host:port (RE Manager is up)."""
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_all_instances(instances: list) -> dict:
    """Probe all instances in parallel. Returns {name: bool} running map."""
    if not instances:
        return {}

    def _probe(inst):
        return inst.get("name", ""), probe_instance_running(
            inst.get("host", ""), inst.get("control_port", 60615)
        )

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(len(instances), 8)
    ) as ex:
        return dict(ex.map(_probe, instances))


# ── profile merge helper ───────────────────────────────────────────────────────

def merge_into_profiles(profiles: list, instances: list) -> tuple:
    """Merge registry instances into a local profiles list.

    Existing profiles matched by name have their host / ports / devices updated.
    New instances are appended as new profiles.
    Returns (updated_profiles, added_count, updated_count).
    """
    from .connection_settings import _PROFILE_DEFAULTS
    by_name = {p["name"]: p for p in profiles}
    added = updated = 0
    for inst in instances:
        name = inst.get("name", "").strip()
        if not name:
            continue
        if name in by_name:
            p = by_name[name]
            for field in ("host", "control_port", "info_port", "doc_port",
                          "procserv_port", "devices_file", "conda_env", "conda_path"):
                if inst.get(field) not in (None, ""):
                    p[field] = inst[field]
            updated += 1
        else:
            profiles.append({
                "name":         name,
                "host":         inst.get("host", ""),
                "devices_file": inst.get("devices_file", "devices.py"),
                "is_local":     False,
                "control_port": inst.get("control_port",  _PROFILE_DEFAULTS["control_port"]),
                "info_port":    inst.get("info_port",     _PROFILE_DEFAULTS["info_port"]),
                "doc_port":     inst.get("doc_port",      _PROFILE_DEFAULTS["doc_port"]),
                "procserv_port":inst.get("procserv_port", _PROFILE_DEFAULTS["procserv_port"]),
                "conda_env":    inst.get("conda_env",    ""),
                "conda_path":   inst.get("conda_path",   "~/miniconda3"),
            })
            by_name[name] = profiles[-1]
            added += 1
    return profiles, added, updated
