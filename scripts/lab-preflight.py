"""Read-only admission checks for a hardware-accelerated development lab."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess

GIB = 1024 ** 3


def cpu_allocation(rows, allowed, vcpus, reserve_cores):
    groups = {}
    for row in rows:
        if str(row.get("online", "yes")).lower() not in ("yes", "true", "1"):
            continue
        key = (int(row["socket"]), int(row["core"]))
        groups.setdefault(key, []).append(int(row["cpu"]))
    complete = [sorted(cpus) for _, cpus in sorted(groups.items())
                if set(cpus).issubset(allowed)]
    reserved = complete[:reserve_cores]
    chosen = []
    for group in complete[reserve_cores:]:
        chosen.extend(group)
        if len(chosen) >= vcpus:
            break
    if len(reserved) != reserve_cores or len(chosen) < vcpus:
        raise ValueError("insufficient complete CPU cores after the desktop reserve")
    return {"lab_cpus": chosen, "reserved_cores": reserved,
            "selection_applied": False}


def capacity_errors(disk_free, memory_available, memory_mib, overhead_mib,
                    disk_reserve_gib, memory_reserve_gib):
    errors = []
    if disk_free < disk_reserve_gib * GIB:
        errors.append("disk free space is below the reserve")
    required = (memory_mib + overhead_mib) * 1024 ** 2 + memory_reserve_gib * GIB
    if memory_available < required:
        errors.append("available RAM cannot cover the guest, QEMU overhead and reserve")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-mib", type=int, default=8192)
    parser.add_argument("--qemu-overhead-mib", type=int, default=2048)
    parser.add_argument("--vcpus", type=int, default=4)
    parser.add_argument("--reserve-cores", type=int, default=2)
    parser.add_argument("--disk-reserve-gib", type=int, default=21)
    parser.add_argument("--memory-reserve-gib", type=int, default=4)
    parser.add_argument("--nix", help="Nix client executable when it is not on PATH")
    args = parser.parse_args()
    budgets = {key: value for key, value in vars(args).items() if key != "nix"}
    if any(value <= 0 for value in budgets.values()):
        parser.error("all resource limits must be positive")
    result = {"schema_version": 1, "status": "blocked", "errors": [],
              "kernel": platform.release(), "architecture": platform.machine(),
              "budgets": budgets, "resource_limits_enforced": False,
              "host_configuration_changed": False}
    errors = result["errors"]
    try:
        if platform.machine() != "x86_64":
            errors.append("the current lab supports only x86_64")
        memory = {line.split(":", 1)[0]: int(line.split()[1]) * 1024
                  for line in Path("/proc/meminfo").read_text().splitlines()}
        result["memory_available_bytes"] = memory["MemAvailable"]
        result["disk_free_bytes"] = {
            "home": shutil.disk_usage(Path.home()).free,
            "nix_store": shutil.disk_usage("/nix/store").free,
            "temporary": shutil.disk_usage("/tmp").free,
        }
        errors.extend(capacity_errors(min(result["disk_free_bytes"].values()),
                                      memory["MemAvailable"], args.memory_mib,
                                      args.qemu_overhead_mib, args.disk_reserve_gib,
                                      args.memory_reserve_gib))
        topology = subprocess.run(
            ["lscpu", "--json", "--extended=CPU,CORE,SOCKET,NODE,ONLINE"],
            check=True, capture_output=True, text=True, timeout=10)
        rows = json.loads(topology.stdout)["cpus"]
        result["cpu_topology"] = rows
        result["cpu_allocation"] = cpu_allocation(
            rows, os.sched_getaffinity(0), args.vcpus, args.reserve_cores)
        flags = Path("/proc/cpuinfo").read_text()
        vendor = "amd" if "AuthenticAMD" in flags else "intel" if "GenuineIntel" in flags else None
        result["cpu_vendor"] = vendor
        if vendor is None:
            errors.append("unknown CPU virtualization vendor")
        else:
            module = "kvm_amd" if vendor == "amd" else "kvm_intel"
            nested = Path(f"/sys/module/{module}/parameters/nested").read_text().strip()
            result["host_nested_parameter"] = nested
            if nested.lower() not in ("1", "y"):
                errors.append("host nested virtualization is disabled")
        info = Path("/dev/kvm").stat()
        if not stat.S_ISCHR(info.st_mode):
            errors.append("/dev/kvm is not a character device")
        else:
            fd = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
            try:
                result["kvm_api"] = fcntl.ioctl(fd, 0xAE00, 0)
                if result["kvm_api"] != 12:
                    errors.append("unexpected KVM API version")
            finally:
                os.close(fd)
        nix = shutil.which(args.nix or "nix")
        if nix is None:
            errors.append("Nix is unavailable on PATH")
        else:
            result["nix_executable"] = nix
            result["nix_version"] = subprocess.run(
                [nix, "--version"], check=True, capture_output=True, text=True,
                timeout=10).stdout.strip()
        controllers = Path("/sys/fs/cgroup/cgroup.controllers")
        result["host_cgroup_v2_controllers"] = (
            controllers.read_text().split() if controllers.exists() else [])
        result["cgroup_delegation_verified"] = False
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        errors.append(str(error))
    result["status"] = "ready" if not errors else "blocked"
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
