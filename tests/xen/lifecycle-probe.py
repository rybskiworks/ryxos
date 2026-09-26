"""Persist a benign boot marker and report readiness on a disposable Xen console."""

import errno
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid


PREFIX = "RYXOS_XEN_LIFECYCLE_READY "


def probe_egress(socket_factory=socket.socket):
    """Require an actual no-route error; timeout is not proof of isolation."""
    with socket_factory(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(2.0)
        try:
            connection.connect(("192.0.2.1", 9))
        except OSError as error:
            if error.errno != errno.ENETUNREACH:
                raise RuntimeError("The egress probe did not return kernel ENETUNREACH") from error
        else:
            raise RuntimeError("The egress probe unexpectedly connected")
    return {"status": "denied", "attempted": True, "transport": "tcp",
            "address": "192.0.2.1", "port": 9, "timeout_seconds": 2,
            "errno": errno.ENETUNREACH, "error_name": "ENETUNREACH"}


def record_boot(directory, boot_id, run=subprocess.run, *, egress=None):
    boot_id = str(uuid.UUID(boot_id))
    previous_path = directory / "result.json"
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else None
    if previous is not None:
        if (previous.get("schema") != 1 or previous.get("status") != "passed"
                or type(previous.get("boot_count")) is not int or previous["boot_count"] < 1):
            raise RuntimeError("Invalid retained lifecycle marker")
        marker = str(uuid.UUID(previous["marker"]))
        count = previous["boot_count"] + (previous["guest_boot_id"] != boot_id)
    else:
        marker, count = str(uuid.uuid4()), 1
    command = run([sys.executable, "-I", "-c", "print('RYXOS_XEN_LIFECYCLE_OK')"],
                  check=True, capture_output=True, text=True, timeout=5)
    if command.stdout.strip() != "RYXOS_XEN_LIFECYCLE_OK":
        raise RuntimeError("Unexpected fixed guest command result")
    result = {"schema": 1, "status": "passed", "guest_boot_id": boot_id,
              "marker": marker, "boot_count": count, "marker_created": previous is None,
              "command_output": command.stdout.strip(), "command_exit_code": command.returncode,
              "network_interfaces": sorted(path.name for path in Path("/sys/class/net").iterdir())}
    if egress is not None:
        result["egress_probe"] = egress
    temporary = directory / "result.new"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(result, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, previous_path)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return result


def main():
    if not Path("/run/current-system").is_dir():
        raise RuntimeError("The NixOS system profile is not active")
    result = record_boot(Path("/var/lib/ryxos-lifecycle-probe"),
                         Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                         egress=probe_egress())
    if result["network_interfaces"] != ["lo"]:
        raise RuntimeError("The lifecycle guest must have only loopback")
    running = True

    def stop(_number, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Repeat a fixed receipt so readiness never depends on an attachment race.
    with Path("/dev/hvc0").open("w", buffering=1) as console:
        while running:
            console.write(PREFIX + json.dumps(result, sort_keys=True) + "\n")
            time.sleep(2)


if __name__ == "__main__":
    main()
