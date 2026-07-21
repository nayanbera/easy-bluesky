"""ssh_manager.py — SSH-based remote RE Manager control (key auth only, no passwords)."""

import time
from pathlib import Path
from .connection_settings import profile_slug


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


def _instance_files(profile_name: str) -> tuple:
    """Return (remote_script, log_file, pid_file) for the given profile name."""
    slug = profile_slug(profile_name)
    return (
        f"/tmp/_easy_bluesky_{slug}.sh",
        f"/tmp/re-manager-{slug}.log",
        f"/tmp/re-manager-{slug}-procserv.pid",
    )


def restart_re_manager(settings: dict, profile: dict) -> tuple:
    """
    SSH into the remote RE Manager host and restart the instance for *profile*.

    Uses procServ when available (ideal for EPICS beamlines — survives SSH
    disconnect reliably). Falls back to systemd-run --user --scope, then nohup.

    If settings['ssh_service'] is set, restarts via systemctl --user instead.

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

        scripts_path  = "$HOME/.easy_bluesky/scripts"
        startup_script = "re_startup_mongo.py"
        devices_file   = profile.get("devices_file", "devices.py")
        ctrl_port      = profile.get("control_port", 60615)
        info_port      = profile.get("info_port", 60625)
        procserv_port  = profile.get("procserv_port", 60635)
        profile_name   = profile.get("name", "Default")
        instance_name  = f"RE-{profile_name}"

        remote_script, log_file, pid_file = _instance_files(profile_name)

        # Write launcher script via SFTP — avoids all shell-quoting issues.
        # It sources .bash_profile for EPICS env vars, exports EASY_BLUESKY_DEVICES_FILE,
        # then exec's start-re-manager.
        script_body = (
            "#!/bin/bash\n"
            "source ~/.bash_profile 2>/dev/null || source ~/.bashrc 2>/dev/null\n"
            f"export EASY_BLUESKY_DEVICES_FILE={devices_file}\n"
            f"exec {exe}"
            f" --zmq-control-addr tcp://*:{ctrl_port}"
            f" --zmq-info-addr tcp://*:{info_port}"
            f" --zmq-publish-console ON"
            f" --startup-script {scripts_path}/{startup_script}"
            f" --existing-plans-devices {scripts_path}/existing_plans_and_devices.yaml"
            f" --user-group-permissions {scripts_path}/user_group_permissions.yaml"
            f" >> {log_file} 2>&1\n"
        )
        sftp = client.open_sftp()
        with sftp.open(remote_script, "w") as f:
            f.write(script_body)
        sftp.chmod(remote_script, 0o755)
        sftp.close()

        # ── Stop ──────────────────────────────────────────────────────────────
        # Run stop in its own SSH channel.  pkill -f matches processes whose
        # cmdline contains the pattern string — which INCLUDES the bash process
        # that is running this very SSH command (bash's cmdline contains
        # "start-re-manager" as part of the pkill argument).  Bash therefore
        # receives SIGTERM and may die early; that is intentional and harmless
        # here because pkill has already dispatched signals to all matching RE
        # Manager processes before bash exits.
        stop_cmd = (
            f"kill $(cat {pid_file} 2>/dev/null) 2>/dev/null; "
            f"rm -f {pid_file}; "
            f"pkill -f start-re-manager 2>/dev/null; "
            f"true"
        )
        _, stdout, _ = client.exec_command(stop_cmd, timeout=12)
        try:
            stdout.channel.recv_exit_status()
        except Exception:
            pass   # bash self-terminated; RE Managers are still being killed

        time.sleep(2)  # let old processes fully exit before binding the ports

        # ── Start ─────────────────────────────────────────────────────────────
        # Fresh channel — bash cmdline is just the procServ invocation, so no
        # self-match risk.  procServ daemonizes on its own; the nohup+setsid
        # fallback achieves the same effect when procServ is absent.
        run_cmd = (
            f"if command -v procServ &>/dev/null; then "
            f"  procServ --noautorestart -n {instance_name}"
            f" -L {log_file} -p {pid_file} {procserv_port}"
            f" /bin/bash {remote_script}; "
            f"else "
            f"  nohup setsid bash {remote_script} >> {log_file} 2>&1 & echo $! > {pid_file}; "
            f"fi"
        )
        _, stdout, stderr = client.exec_command(run_cmd, timeout=15)
        stdout.channel.recv_exit_status()
        client.close()
        env_note = f" (conda env: {settings['conda_env']})" if settings.get("conda_env") else ""
        return True, f"RE Manager (profile: {profile_name}) restarted on remote host{env_note}"

    except Exception as e:
        try:
            client.close()
        except Exception:
            pass
        return False, f"SSH command failed: {e}"


def stop_re_manager(settings: dict, profile: dict) -> tuple:
    """SSH into the remote host and kill the RE Manager instance for *profile*."""
    try:
        client = _get_client(settings)
    except Exception as e:
        return False, str(e)

    service = settings.get("ssh_service", "").strip()
    try:
        if service:
            cmd = f"systemctl --user stop {service}"
        else:
            profile_name = profile.get("name", "Default")
            ctrl_port = profile.get("control_port", 60615)
            _, log_file, pid_file = _instance_files(profile_name)
            cmd = (
                # Kill the specific instance we launched (via pid file),
                # then kill ALL remaining start-re-manager processes so
                # stale instances from previous sessions can't impersonate
                # this profile on the same control port.
                f"if [ -f {pid_file} ]; then "
                f"  kill $(cat {pid_file}) 2>/dev/null; "
                f"  rm -f {pid_file}; "
                f"fi; "
                f"pkill -f start-re-manager 2>/dev/null; "
                f"sleep 1; true"
            )
        _, stdout, stderr = client.exec_command(cmd, timeout=10)
        stdout.channel.recv_exit_status()
        client.close()
        return True, "RE Manager stopped on remote host"
    except Exception as e:
        try:
            client.close()
        except Exception:
            pass
        return False, f"SSH stop failed: {e}"


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


def test_ssh_connection(settings: dict) -> tuple:
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

            # Also report procServ availability
            _, stdout3, _ = client.exec_command(
                "which procServ 2>/dev/null && procServ --version 2>&1 | head -1", timeout=5
            )
            procserv_info = stdout3.read().decode().strip()
            client.close()
            ps_note = f"  |  {procserv_info}" if procserv_info else "  |  procServ not found"
            return True, f"SSH OK  |  start-re-manager found in env '{env}'{ps_note}"

        client.close()
        return True, "SSH connection OK"

    except Exception as e:
        try:
            client.close()
        except Exception:
            pass
        return False, str(e)
