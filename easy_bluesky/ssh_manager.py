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


def _conda_prefix(settings: dict) -> str:
    """
    Return a shell prefix that runs a command inside the configured conda env.

    With conda_env='bluesky' and conda_path='~/miniconda3' this produces:
        $HOME/miniconda3/bin/conda run -n bluesky --no-capture-output

    Returns '' when conda_env is not configured (command runs on PATH as-is).
    """
    env  = settings.get("conda_env", "").strip()
    base = settings.get("conda_path", "~/miniconda3").strip() or "~/miniconda3"
    if not env:
        return ""
    # Replace ~ with $HOME so the remote shell expands it correctly.
    # Literal ~ is not expanded by the non-interactive SSH shell.
    base = base.replace("~", "$HOME")
    # --no-capture-output keeps stdout/stderr visible in the SSH channel
    return f"{base}/bin/conda run -n {env} --no-capture-output "


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
    prefix  = _conda_prefix(settings)
    try:
        if service:
            # systemd --user services don't need conda; they use the ExecStart
            # path already. We still support it in case the user sets it up
            # differently.
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

            stop_cmd  = "pkill -f start-re-manager; sleep 1"
            start_cmd = (
                f"nohup {prefix}start-re-manager"
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
            env_note = f" (conda env: {settings['conda_env']})" if settings.get("conda_env") else ""
            return True, f"RE Manager restarted on remote host{env_note}"

    except Exception as e:
        try:
            client.close()
        except Exception:
            pass
        return False, f"SSH command failed: {e}"


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

        # If a conda env is configured, verify it exists on the remote
        env = settings.get("conda_env", "").strip()
        if env:
            base = settings.get("conda_path", "~/miniconda3").strip() or "~/miniconda3"
            check_cmd = f"{base}/bin/conda env list | grep -q '^{env} '"
            _, _, stderr = client.exec_command(check_cmd, timeout=10)
            exit_code = stderr.channel.recv_exit_status()
            if exit_code != 0:
                client.close()
                return False, (
                    f"SSH OK, but conda env '{env}' not found at {base}.\n"
                    f"Check Conda path and env name in Connection Settings."
                )
            client.close()
            return True, f"SSH OK  |  conda env '{env}' found"

        client.close()
        return True, "SSH connection OK"

    except Exception as e:
        try:
            client.close()
        except Exception:
            pass
        return False, str(e)
