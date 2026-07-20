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


def _re_manager_exe(settings: dict) -> str:
    """
    Return the path to start-re-manager on the remote host.

    When conda_env is set, constructs the full path directly into the env's
    bin directory — no 'conda run' or activation needed:
        {conda_path}/envs/{conda_env}/bin/start-re-manager

    Falls back to bare 'start-re-manager' (relies on PATH) when not set.
    ~ is replaced with $HOME for safe remote-shell expansion.
    """
    env  = settings.get("conda_env", "").strip()
    base = settings.get("conda_path", "").strip().replace("~", "$HOME")
    if env and base:
        return f"{base}/envs/{env}/bin/start-re-manager"
    return "start-re-manager"


def restart_re_manager(settings: dict, sim: bool = False) -> tuple[bool, str]:
    """
    SSH into the remote RE Manager host and restart it.

    If settings['ssh_service'] is set restarts via:
        systemctl --user restart <service>
    Otherwise uses pkill + nohup start-re-manager.

    Conda env is handled automatically when settings['conda_env'] is set.

    Returns (success, message).
    """
    try:
        client = _get_client(settings)
    except Exception as e:
        return False, str(e)

    service = settings.get("ssh_service", "").strip()
    exe     = _re_manager_exe(settings)
    try:
        if service:
            cmd = f"systemctl --user restart {service}"
            _, stdout, stderr = client.exec_command(cmd, timeout=15)
            stdout.channel.recv_exit_status()
            err = stderr.read().decode().strip()
            client.close()
            if err:
                return False, f"systemctl: {err}"
            return True, f"systemctl --user restart {service} OK"
        else:
            script       = "re_startup_sim.py" if sim else "re_startup_mongo.py"
            scripts_path = "$HOME/.easy_bluesky/scripts"

            # Write a launcher script via SFTP so we avoid all shell-quoting
            # issues. The script sources .bash_profile to get EPICS vars, then
            # runs start-re-manager in the background.
            script_body = (
                "#!/bin/bash\n"
                "source ~/.bash_profile 2>/dev/null || source ~/.bashrc 2>/dev/null\n"
                f"exec {exe}"
                f" --zmq-publish-console ON"
                f" --startup-script {scripts_path}/{script}"
                f" --existing-plans-devices {scripts_path}/existing_plans_and_devices.yaml"
                f" --user-group-permissions {scripts_path}/user_group_permissions.yaml"
                f" >> /tmp/re-manager.log 2>&1\n"
            )
            remote_script = "/tmp/_easy_bluesky_start.sh"
            sftp = client.open_sftp()
            with sftp.open(remote_script, "w") as f:
                f.write(script_body)
            sftp.chmod(remote_script, 0o755)
            sftp.close()

            # Kill existing instance, then launch via the script
            stop_cmd = "pkill -f start-re-manager; sleep 1"
            run_cmd  = f"nohup bash {remote_script} > /dev/null 2>&1 &"
            _, stdout, stderr = client.exec_command(
                f"{stop_cmd}; {run_cmd}", timeout=15
            )
            stdout.channel.recv_exit_status()
            client.close()
            env_note = f" (conda env: {settings['conda_env']})" if settings.get("conda_env") else ""
            return True, f"RE Manager restarted on remote host{env_note}"

    except Exception as e:
        try:
            client.close()
        except Exception:
            pass
        return False, f"SSH command failed: {e}"


def wait_for_port(host: str, port: int, timeout: int = 30) -> bool:
    """Poll host:port every 2 s until it accepts a connection or timeout expires."""
    import socket, time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(2)
    return False


def test_ssh_connection(settings: dict) -> tuple[bool, str]:
    """
    Verify SSH connectivity and optionally check that the conda env exists.
    Returns (success, message).
    """
    try:
        client = _get_client(settings)
    except Exception as e:
        return False, str(e)

    try:
        # Basic connectivity
        _, stdout, _ = client.exec_command("echo ok", timeout=5)
        if stdout.read().decode().strip() != "ok":
            client.close()
            return False, "Unexpected response to echo"

        # If a conda env is configured, verify start-re-manager exists there
        env = settings.get("conda_env", "").strip()
        if env:
            exe = _re_manager_exe(settings)
            check_cmd = f"test -x {exe} && echo found"
            _, stdout2, _ = client.exec_command(check_cmd, timeout=10)
            result = stdout2.read().decode().strip()
            if result != "found":
                client.close()
                return False, (
                    f"SSH OK, but start-re-manager not found at:\n{exe}\n"
                    f"Check Conda path and env name in Connection Settings."
                )
            client.close()
            return True, f"SSH OK  |  start-re-manager found in env '{env}'"

        client.close()
        return True, "SSH connection OK"

    except Exception as e:
        try:
            client.close()
        except Exception:
            pass
        return False, str(e)
