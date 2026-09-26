"""Boot and optional KVM probes for a disposable, credential-free Xen guest."""

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


def probe_mode(cmdline: str) -> str:
    modes = [word.split("=", 1)[1] for word in cmdline.split() if word.startswith("ryxos.probe=")]
    if not modes:
        return "nested"
    if len(modes) != 1 or modes[0] not in {"boot", "nested"}:
        raise RuntimeError("Expected one supported ryxos.probe mode")
    return modes[0]


def probe(mode: str = "nested") -> dict:
    result = {"schema": 1, "status": "failed", "mode": mode, "vm_booted": False}
    try:
        if mode == "boot":
            if not Path("/run/current-system").is_dir():
                raise RuntimeError("NixOS has not activated its system profile")
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            if str(uuid.UUID(boot_id)) != boot_id:
                raise RuntimeError("Guest boot identity is not canonical")
            command = subprocess.run(
                [sys.executable, "-c", "print('RYXOS_XEN_BOOT_OK')"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if command.stdout.strip() != "RYXOS_XEN_BOOT_OK":
                raise RuntimeError("Guest command returned an unexpected response")
            result.update(
                status="passed",
                guest_booted=True,
                guest_boot_id=boot_id,
                command_output=command.stdout.strip(),
                command_exit_code=command.returncode,
                nested_probe="not_requested",
            )
            return result
        if mode != "nested":
            raise RuntimeError("Unsupported probe mode")
        cpuinfo = Path("/proc/cpuinfo").read_text()
        flags = set()
        for line in cpuinfo.splitlines():
            if line.startswith("flags") and ":" in line:
                flags.update(line.split(":", 1)[1].split())
        if "svm" in flags:
            module = "kvm_amd"
            result["vendor"] = "amd"
        elif "vmx" in flags:
            module = "kvm_intel"
            result["vendor"] = "intel"
        else:
            raise RuntimeError("Xen guest CPU exposes neither SVM nor VMX")
        subprocess.run(["modprobe", module], check=True, timeout=15)
        result["module"] = module
        result["nested_parameter"] = Path(f"/sys/module/{module}/parameters/nested").read_text().strip()
        if result["nested_parameter"].lower() not in ("1", "y"):
            raise RuntimeError("KVM nested parameter is disabled")
        kvm_fd = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
        try:
            result["kvm_api"] = fcntl.ioctl(kvm_fd, 0xAE00, 0)
            if result["kvm_api"] != 12:
                raise RuntimeError("Unexpected KVM API version")
            vm_fd = fcntl.ioctl(kvm_fd, 0xAE01, 0)
            os.close(vm_fd)
            result["empty_vm_created_and_closed"] = True
        finally:
            os.close(kvm_fd)
        result["status"] = "passed"
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
    return result


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: probe.py RESULT_JSON")
    try:
        result = probe(probe_mode(Path("/proc/cmdline").read_text()))
    except (OSError, RuntimeError) as error:
        result = {"schema": 1, "status": "failed", "vm_booted": False, "error": str(error)}
    target = Path(sys.argv[1])
    with target.open("w") as stream:
        os.chmod(target, 0o600)
        json.dump(result, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
