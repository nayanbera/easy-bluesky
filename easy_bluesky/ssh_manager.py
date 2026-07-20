"""ssh_manager.py — SSH-based remote RE Manager control (key auth only, no passwords)."""

from pathlib import Path


def _get_client(settings: dict):
    """Return a connected paramiko SSHClient using key authentication."""
    try:
        import paramiko
    except ImportError:
        raise RuntimeError(
            "paramiko is not installed.\n"
            "Run:  pip install paramiko"
        )

    host     = settings["host"]
    port     = settings.get("ssh_port", 22)
    user     = settings.get("ssh_user", "")
    key_path = settings.get("ssh_key_path", "")

    if not user:
        raise ValueError("SSH user is not configured. Open Connection Settings.")
    if not key_path:
        raise ValueError("SSH key path is not configured. Open Connection Settings.")

    key_file = Path(key_path).expanduser()
    if not key_file.exists():
        raise FileNotFoundError(
            f"SSH private key not found: {key_file}\n"
            "Generate one with:  ssh-keygen -t ed25519\n"
            "Then copy the public key to the remote machine:\n"
            f"  ssh-copy-id -i {key_file}.pub {user}@{host}"
        )

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=port,
        username=user,
        key_filename=str(key_file),
        timeout=10,
    )
    return client


def restart_re_manager(settings: dict, sim: bool = False) -> tuple[bool, str]:
    """
    SSH into the remote RE Manager host and restart it.

    If settings['ssh_service'] is set (e.g. 're-manager'), restarts via systemd:
        sudo systemctl restart <service>

    Otherwise uses a direct pkill + nohup approach, assuming the remote machine
    has easy-bluesky installed and scripts in ~/.easy_bluesky/scripts/.

    Returns (success, message).
    """
    try:
        client = _get_client(settings)
    except Exception as e:
        return False, str(e)

    service = settings.get("ssh_service", "").strip()
    try:
        if service:
            # systemd path
            cmd = f"sudo systemctl restart {service}"
            _, stdout, stderr = client.exec_command(cmd, timeout=15)
            stdout.channel.recv_exit_status()
            err = stderr.read().decode().strip()
            client.close()
            if err:
                return False, f"systemctl: {err}"
            return True, f"systemctl restart {service} OK"
        else:
            # Direct path: kill existing process, start fresh
            script = "re_startup_sim.py" if sim else "re_startup_mongo.py"
            scripts_path = "~/.easy_bluesky/scripts"

            stop_cmd = "pkill -f start-re-manager; sleep 1"
            start_cmd = (
                f"nohup start-re-manager"
                f" --zmq-publish-console ON"
                f" --startup-script {scripts_path}/{script}"
                f" --existing-plans-devices {scripts_path}/existing_plans_and_devices.yaml"
                f" --user-group-permissions {scripts_path}/user_group_permissions.yaml"
                f" > /tmp/re-manager.log 2>&1 &"
            )
            cmd = f"{stop_cmd}; {start_cmd}"
            _, stdout, stderr = client.exec_command(cmd, timeout=15)
            stdout.channel.recv_exit_status()
            client.close()
            return True, "RE Manager restarted on remote host"

    except Exception as e:
        try:
            client.close()
        except Exception:
            pass
        return False, f"SSH command failed: {e}"


def test_ssh_connection(settings: dict) -> tuple[bool, str]:
    """Return (success, message) for a quick SSH connectivity check."""
    try:
        client = _get_client(settings)
        _, stdout, _ = client.exec_command("echo ok", timeout=5)
        out = stdout.read().decode().strip()
        client.close()
        return (True, "SSH connection OK") if out == "ok" else (False, f"Unexpected response: {out}")
    except Exception as e:
        return False, str(e)
