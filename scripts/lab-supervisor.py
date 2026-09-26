"""Run one reviewed NixOS test driver under verified host resource limits."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import runpy
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

GIB = 1024 ** 3
MIB = 1024 ** 2
PREFLIGHT = runpy.run_path(str(Path(__file__).with_name("lab-preflight.py")))
NETWORK_PROFILES = ("closed", "loopback-ssh")
SSH_NETDEV = "user,id=ssh0,restrict=on,ipv6=off,hostfwd=tcp:127.0.0.1:22222-10.0.2.15:22222"
SSH_DEVICE = "virtio-net-pci,netdev=ssh0,mac=52:54:00:72:78:01"


class Refusal(RuntimeError):
    pass


def network_policy(profile):
    if profile not in NETWORK_PROFILES:
        raise Refusal("unknown network admission profile")
    return {"profile": profile, "host_forwards": [] if profile == "closed" else [{
        "protocol": "tcp", "host_address": "127.0.0.1", "host_port": 22222,
        "guest_address": "10.0.2.15", "guest_port": 22222,
        "netdev": SSH_NETDEV, "device": SSH_DEVICE,
    }]}


def request_network_policy(request):
    if "network_policy" not in request:
        # Old closed-network leases remain recoverable with their existing checks.
        recorded = network_policy("closed")
    else:
        recorded = request["network_policy"]
    if not isinstance(recorded, dict):
        raise Refusal("malformed recorded network policy")
    expected = network_policy(recorded.get("profile"))
    if json.dumps(recorded, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise Refusal("recorded network mapping does not match its fixed profile")
    admission = request.get("driver_admission")
    if admission is not None or expected["profile"] == "loopback-ssh":
        if not isinstance(admission, dict) or admission.get("network_policy") != expected:
            raise Refusal("recorded network profile lacks its matching driver admission")
    return expected


def inspect_loopback_arguments(arguments, source):
    """Admit only the reviewed single-dom0 launcher shape, not general QEMU flags."""
    tokens = [item for item in arguments if item != "\n"]
    if (tokens[-2:] != ["$QEMU_OPTS", "$@"] or source.count("QEMU_OPTS") != 1
            or "QEMU_NET_OPTS" in source or source.count("$@") != 1):
        raise Refusal("unexpected launcher argument or network environment expansion")
    values, flags = {}, []
    allowed = {"-machine", "-cpu", "-name", "-m", "-smp", "-device", "-nic", "-netdev", "-drive", "-object"}
    index, end = 2, len(tokens) - 2
    while index < end:
        key = tokens[index]
        if key in ("-no-user-config", "-usb", "-nographic"):
            flags.append(key)
            index += 1
        elif key in allowed and index + 1 < end:
            values.setdefault(key, []).append(tokens[index + 1])
            index += 2
        else:
            raise Refusal("unreviewed extra argument in the fixed loopback launcher")
    memory, cpus = values.get("-m", []), values.get("-smp", [])
    if (len(memory) != 1 or not memory[0].isdigit() or int(memory[0]) <= 0
            or len(cpus) != 1 or not cpus[0].isdigit() or int(cpus[0]) <= 0):
        raise Refusal("invalid loopback launcher resource arguments")
    expected = {
        "-machine": ["accel=kvm", "q35", "memory-backend=mem0"],
        "-cpu": ["max", "host"], "-name": ["dom0"], "-m": memory, "-smp": cpus,
        "-nic": ["none"], "-netdev": [SSH_NETDEV],
        "-device": ["virtio-rng-pci", SSH_DEVICE,
                    "virtio-blk-pci,bootindex=1,drive=drive1,serial=root",
                    "virtio-keyboard", "usb-tablet,bus=usb-bus.0"],
        "-object": ["memory-backend-memfd,id=mem0,size=" + memory[0] + "M,share=on"],
    }
    drives = values.pop("-drive", [])
    firmware = (r"if=pflash,format=raw,unit=0,readonly=on,file="
                r"/nix/store/[0-9a-z]{32}-OVMF-[^/\s,]+/FV/OVMF_CODE\.fd")
    if (values != expected or flags != ["-no-user-config", "-usb", "-nographic"]
            or len(drives) != 3
            or drives[0] != "cache=writeback,file=$NIX_DISK_IMAGE,id=drive1,if=none,index=1,werror=report"
            or not re.fullmatch(firmware, drives[1])
            or drives[2] != "if=pflash,format=raw,unit=1,readonly=off,file=$NIX_EFI_VARS"):
        raise Refusal("launcher differs from the fixed loopback NIC, forward or device contract")


def private_directory(path, uid=None):
    uid = os.geteuid() if uid is None else uid
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid
            or info.st_mode & 0o077):
        raise Refusal(f"not an owned private directory: {path}")
    return {"device": info.st_dev, "inode": info.st_ino}


def read_json(path, uid=None):
    uid = os.geteuid() if uid is None else uid
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                or info.st_nlink != 1 or info.st_mode & 0o077
                or info.st_size > MIB):
            raise Refusal(f"not an owned private JSON file: {path}")
        return json.load(stream)


def write_json(path, value, exclusive=False):
    """Persist a private receipt before starting or discarding owned resources."""
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    temporary = path if exclusive else path.with_name(path.name + "." + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if not exclusive:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if not exclusive and temporary.exists():
            temporary.unlink()


def executable(value):
    found = shutil.which(value)
    if not found:
        raise Refusal(f"required executable is unavailable: {value}")
    invocation = Path(found).absolute()
    invocation.resolve(strict=True)
    # Multicall tools such as nix-store select their command from argv[0].
    # Validate the target without replacing the invocation's basename.
    return str(invocation)


def store_file(value, store=Path("/nix/store"), executable_required=False):
    path = Path(value).resolve(strict=True)
    try:
        relative = path.relative_to(store)
    except ValueError as error:
        raise Refusal("the driver and its launchers must be immutable store files") from error
    if not re.fullmatch(r"[0-9a-z]{32}-.+", relative.parts[0]):
        raise Refusal("invalid store path")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222 or info.st_size > MIB:
        raise Refusal(f"store input is not an immutable regular file: {path}")
    if executable_required and not os.access(path, os.X_OK):
        raise Refusal(f"store executable cannot be executed: {path}")
    return path


def inspect_driver(value, memory_mib, vcpus, store=Path("/nix/store"), network_profile="closed"):
    """Recognize the pinned NixOS driver contract; this is not a shell sandbox."""
    policy = network_policy(network_profile)
    driver = store_file(value, store, True)
    text = driver.read_text()
    configs = re.findall(r"--config (" + re.escape(str(store)) + r"/[^\s\"']+)", text)
    if len(configs) != 1:
        raise Refusal("expected one explicit NixOS driver configuration")
    config_path = store_file(configs[0], store)
    config = json.loads(config_path.read_text())
    if config.get("containers") or config.get("enable_ssh_backdoor") or config.get("vlans"):
        raise Refusal("only closed QEMU drivers without containers or SSH backdoors are admitted")
    machines = config.get("vms", {})
    if not machines or len(machines) > 16:
        raise Refusal("expected a bounded nonempty QEMU machine set")
    if network_profile == "loopback-ssh" and list(machines) != ["dom0"]:
        raise Refusal("the fixed loopback profile requires exactly one dom0 machine")
    records = []
    for machine in machines.values():
        launcher = store_file(machine["start_script"], store, True)
        source = launcher.read_text()
        lines = [line for line in source.splitlines() if line.startswith("exec ")]
        if len(lines) != 1 or not re.search(r"/bin/qemu-system-x86_64 -machine accel=kvm(?: |$)", lines[0]):
            raise Refusal("the QEMU launcher must require KVM without an emulation fallback")
        if re.findall(r"accel=([^\s\\]+)", source) != ["kvm"] or re.search(r"-accel\s", source):
            raise Refusal("additional QEMU acceleration overrides are forbidden")
        if "-no-user-config" not in source:
            raise Refusal("the QEMU launcher must disable implicit user configuration")
        arguments = shlex.split(source[source.index(lines[0]):], comments=True)
        if network_profile == "loopback-ssh":
            inspect_loopback_arguments(arguments, source)
        nic_values = []
        for index, argument in enumerate(arguments):
            key = argument.split("=", 1)[0]
            if key in ("-net", "-virtfs", "-fsdev") or (key == "-netdev" and network_profile == "closed"):
                raise Refusal("explicit network backends and host directory shares are forbidden")
            if key in ("-nic", "-device") and "=" not in argument and index + 1 >= len(arguments):
                raise Refusal("the QEMU launcher has an incomplete device argument")
            if key == "-nic":
                nic_values.append(argument.split("=", 1)[1] if "=" in argument else arguments[index + 1])
            if key == "-device":
                device = argument.split("=", 1)[1] if "=" in argument else arguments[index + 1]
                if (device.split(",", 1)[0].startswith(("virtio-net", "virtio-9p", "vhost-user-fs", "vfio-pci", "usb-host"))
                        and not (network_profile == "loopback-ssh" and device == SSH_DEVICE)):
                    raise Refusal("network, shared-filesystem and host-passthrough devices are forbidden")
        if nic_values != ["none"]:
            raise Refusal("the QEMU launcher must explicitly disable all default NICs")
        ram = re.findall(r"^\s+-m (\d+)\s*\\?\s*$", source, re.MULTILINE)
        cpus = re.findall(r"^\s+-smp (\d+)\s*\\?\s*$", source, re.MULTILINE)
        if len(ram) != 1 or len(cpus) != 1:
            raise Refusal("cannot determine the pinned launcher's memory and CPU budget")
        records.append({"launcher": str(launcher), "sha256": hashlib.sha256(source.encode()).hexdigest(),
                        "memory_mib": int(ram[0]), "vcpus": int(cpus[0])})
    if sum(row["memory_mib"] for row in records) > memory_mib:
        raise Refusal("declared guest memory is below the driver machine total")
    if sum(row["vcpus"] for row in records) > vcpus:
        raise Refusal("declared vCPUs are below the driver machine total")
    return {"driver": str(driver), "store_path": str(store / driver.relative_to(store).parts[0]),
            "config": str(config_path), "machines": records, "network_policy": policy}


def retain_driver(output, admitted, nix_store, env, run=subprocess.run):
    roots = output / "roots"
    roots.mkdir(mode=0o700)
    root = roots / "driver"
    # The output must already exist. An admission command must never build/fetch.
    command = [nix_store, "--realise", admitted["store_path"], "--add-root", str(root), "--indirect",
               "--option", "substitute", "false", "--option", "max-jobs", "0", "--option", "builders", ""]
    result = run(command, env=env, capture_output=True, text=True, timeout=30)
    if result.returncode or not root.is_symlink() or root.resolve(strict=True) != Path(admitted["store_path"]):
        write_json(output / "gc-root-error.json", {
            "returncode": result.returncode, "stderr": result.stderr[-8192:],
            "root_is_symlink": root.is_symlink(),
        })
        raise Refusal("could not retain the existing driver closure without building or fetching; inspect gc-root-error.json")
    return {"path": str(root), "store_path": admitted["store_path"], "retained_after_run": True}


def resource_sample(paths, meminfo=Path("/proc/meminfo")):
    memory = {line.split(":", 1)[0]: int(line.split()[1]) * 1024
              for line in meminfo.read_text().splitlines()}
    return {"memory_available_bytes": memory["MemAvailable"],
            "disk_free_bytes": {name: shutil.disk_usage(path).free for name, path in paths.items()}}


def admit(sample, limits, rows, allowed):
    errors = PREFLIGHT["capacity_errors"](
        min(sample["disk_free_bytes"].values()), sample["memory_available_bytes"],
        limits["memory_mib"], limits["qemu_overhead_mib"],
        limits["disk_reserve_gib"], limits["memory_reserve_gib"])
    if errors:
        raise Refusal("; ".join(errors))
    return PREFLIGHT["cpu_allocation"](rows, allowed, limits["vcpus"], limits["reserve_cores"])


def hardware_admission():
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise Refusal("this lab runner requires Linux x86_64 and KVM")
    flags = Path("/proc/cpuinfo").read_text()
    vendor = "amd" if "AuthenticAMD" in flags else "intel" if "GenuineIntel" in flags else None
    if vendor is None:
        raise Refusal("unknown CPU virtualization vendor")
    module = "kvm_amd" if vendor == "amd" else "kvm_intel"
    nested = Path(f"/sys/module/{module}/parameters/nested").read_text().strip()
    if nested.lower() not in ("1", "y"):
        raise Refusal("host nested virtualization must already be enabled")
    if not stat.S_ISCHR(Path("/dev/kvm").stat().st_mode):
        raise Refusal("/dev/kvm is not a character device")
    fd = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
    try:
        api = fcntl.ioctl(fd, 0xAE00, 0)
        if api != 12:
            raise Refusal("unexpected KVM API version")
        vm = fcntl.ioctl(fd, 0xAE01, 0)
        os.close(vm)
    finally:
        os.close(fd)
    return {"vendor": vendor, "nested": nested, "kvm_api": api,
            "empty_vm_created_and_closed": True}


def clean_environment(scratch, output, path):
    # An allowlist also excludes unknown future QEMU overrides and credentials.
    return {"PATH": path, "HOME": str(scratch / "home"), "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8", "TMPDIR": str(scratch / "tmp"),
            "XDG_RUNTIME_DIR": str(scratch / "runtime"),
            "XDG_CONFIG_HOME": str(scratch / "config"),
            "XDG_CACHE_HOME": str(scratch / "cache"),
            "XDG_STATE_HOME": str(scratch / "state"),
            "PYTHONDONTWRITEBYTECODE": "1", "out": str(output)}


def effective_limits(request, cgroup_root=Path("/sys/fs/cgroup"),
                     proc_cgroup=Path("/proc/self/cgroup"), affinity=os.sched_getaffinity):
    rows = [line[3:] for line in proc_cgroup.read_text().splitlines() if line.startswith("0::")]
    if len(rows) != 1 or ".." in Path(rows[0]).parts or Path(rows[0]).name != request["unit"]:
        raise Refusal("not inside the exact owned cgroup-v2 service")
    group = cgroup_root / rows[0].lstrip("/")
    values = {name: (group / name).read_text().strip() if (group / name).exists() else None
              for name in ("cpu.max", "memory.max", "memory.swap.max", "cpuset.cpus.effective", "io.weight", "pids.max")}
    actual_affinity = sorted(affinity(0))
    if actual_affinity != sorted(request["cpus"]):
        raise Refusal("the requested complete-core CPU affinity is not effective")
    try:
        quota, period = map(int, (values["cpu.max"] or "").split())
        valid = (period > 0 and quota == period * request["limits"]["vcpus"]
                 and int(values["memory.max"]) == request["memory_max_bytes"]
                 and values["memory.swap.max"] == "0")
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise Refusal("CPU, memory and zero-swap limits are not effectively delegated")
    cpuset = set()
    for entry in (values["cpuset.cpus.effective"] or "").split(","):
        if entry:
            bounds = list(map(int, entry.split("-")))
            cpuset.update(range(bounds[0], bounds[-1] + 1))
    return {"cgroup": str(group), "values": values, "affinity": actual_affinity,
            "cpu_quota_verified": True, "memory_verified": True, "swap_verified": True,
            "cpuset_matches_allocation": cpuset == set(request["cpus"]),
            "io_weight_requested": 20, "io_weight_applied": values["io.weight"] == "default 20",
            "io_bandwidth_cap": None, "affinity_is_hostile_code_boundary": False}


class Controller:
    def __init__(self, tools, runtime, run=subprocess.run, cgroup_root=Path("/sys/fs/cgroup")):
        self.tools, self.run = tools, run
        self.cgroup_root = cgroup_root
        self.env = {"PATH": tools["path"], "HOME": str(runtime), "LANG": "C.UTF-8",
                    "XDG_RUNTIME_DIR": str(runtime),
                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(runtime / "bus")}

    def observe(self, unit):
        result = self.run([self.tools["systemctl"], "--user", "show", unit,
                           "--property=LoadState,ActiveState,Description,Transient,ControlGroup"],
                          env=self.env, capture_output=True, text=True, timeout=10)
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if values.get("LoadState") == "not-found":
            return values
        if result.returncode or not values:
            raise Refusal("cannot verify owned service state")
        return values

    def stop_owned(self, request):
        state = self.observe(request["unit"])
        if state.get("LoadState") == "not-found":
            if request.get("launch_state") not in ("not_submitted", "settled"):
                inside_path = Path(request["run_dir"]) / "inside.json"
                inside = read_json(inside_path) if inside_path.exists() else {}
                # A pending systemd-run client may create its unit after this
                # observation. Entry into the inside verifier proves that its
                # one submission happened; otherwise preserve recovery state.
                if inside.get("schema_version") != 1 or type(inside.get("driver_started")) is not bool:
                    raise Refusal("service submission is unsettled; retaining the lease and scratch for inspection")
            self.check_empty(request, None)
            return {"gone": True, "already_absent": True}
        if (state.get("Description") != request["description"]
                or state.get("Transient") != "yes"
                or (state.get("ControlGroup") and Path(state["ControlGroup"]).name != request["unit"])):
            raise Refusal("service ownership changed; refusing to stop it")
        group = state.get("ControlGroup")
        stopped = self.run([self.tools["systemctl"], "--user", "stop", request["unit"]],
                           env=self.env, capture_output=True, text=True, timeout=25)
        state = self.observe(request["unit"])
        gone = state.get("LoadState") == "not-found" or state.get("ActiveState") in ("inactive", "failed")
        if not gone:
            raise Refusal("owned lab service is still active")
        self.check_empty(request, group)
        return {"gone": True, "stop_exit_code": stopped.returncode, "state": state}

    def check_empty(self, request, group):
        inside_path = Path(request["run_dir"]) / "inside.json"
        if inside_path.exists():
            recorded = read_json(inside_path).get("effective_limits", {}).get("cgroup")
            if recorded:
                group = str(Path(recorded).relative_to(self.cgroup_root))
        if group:
            if ".." in Path(group).parts or Path(group).name != request["unit"]:
                raise Refusal("unexpected cgroup ownership path")
            path = self.cgroup_root / group.lstrip("/")
            if path.exists():
                events = dict(line.split() for line in (path / "cgroup.events").read_text().splitlines())
                if events.get("populated") != "0":
                    raise Refusal("owned cgroup still contains processes; retaining state")


class Slot:
    def __init__(self, runtime):
        private_directory(runtime)
        self.directory = runtime / "ryxos-labs"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        private_directory(self.directory)
        self.active = self.directory / "active.json"
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.directory / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        info = os.fstat(self.fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            os.close(self.fd)
            raise Refusal("unsafe lab lock file")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(self.fd)
            raise Refusal("another lab supervisor holds the user lock") from error
        return self

    def __exit__(self, *_):
        os.close(self.fd)


def remove_scratch(request):
    scratch = Path(request["scratch"])
    if not scratch.exists():
        return {"removed": True, "already_absent": True}
    if (not scratch.name.startswith("ryxos-lab-")
            or private_directory(scratch) != request["scratch_identity"]
            or read_json(scratch / "ownership.json")["nonce"] != request["nonce"]):
        raise Refusal("scratch identity changed; retaining it")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise Refusal("safe directory-relative removal is unavailable")
    shutil.rmtree(scratch)
    return {"removed": True}


def cleanup(request, slot, controller, launch_process=None):
    lease = read_json(slot.active)
    if lease["nonce"] != request["nonce"] or lease["run_dir"] != request["run_dir"]:
        raise Refusal("active lab ownership does not match this run")
    unit = controller.stop_owned(request)
    if launch_process is not None:
        launch_process.wait(timeout=10)
    scratch = remove_scratch(request)
    slot.active.unlink()
    return {"unit": unit, "scratch": scratch, "lease_removed": True}


def service_command(request, tools):
    limits = request["limits"]
    properties = {
        "Description": request["description"], "CPUQuota": str(100 * limits["vcpus"]) + "%",
        "CPUAffinity": " ".join(map(str, request["cpus"])),
        "AllowedCPUs": ",".join(map(str, request["cpus"])),
        "MemoryMax": str(request["memory_max_bytes"]), "MemorySwapMax": "0",
        "IOWeight": "20", "TasksMax": "1024", "RuntimeMaxSec": str(limits["timeout_seconds"]),
        "TimeoutStopSec": "15", "KillMode": "control-group", "UMask": "0077",
    }
    command = [tools["systemd_run"], "--user", "--wait", "--pipe", "--collect", "--quiet",
               "--service-type=exec", "--unit", request["unit"]]
    for name, value in properties.items():
        command.extend(["--property", name + "=" + value])
    command += [tools["env"], "-i", "PATH=" + tools["path"], "PYTHONDONTWRITEBYTECODE=1",
                tools["python"], "-I", str(Path(__file__).resolve()), "--inside", str(Path(request["run_dir"]) / "request.json")]
    return command


def submit(request, tools, environment, log):
    # Persist intent before Popen: a signal can arrive after fork but before
    # Python receives the launcher process object.
    request["launch_state"] = "submitting"
    write_json(Path(request["run_dir"]) / "request.json", request)
    return subprocess.Popen(service_command(request, tools), env=environment,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)


def inside(request_path, verify=effective_limits, run=subprocess.run):
    request = read_json(request_path)
    output, scratch = Path(request["run_dir"]), Path(request["scratch"])
    private_directory(output)
    if private_directory(scratch) != request["scratch_identity"]:
        raise Refusal("scratch ownership changed before launch")
    receipt = {"schema_version": 1, "driver_started": False, "status": "blocked"}
    try:
        receipt["network_policy"] = request_network_policy(request)
        receipt["effective_limits"] = verify(request)
        if receipt["network_policy"]["profile"] == "loopback-ssh":
            admitted = inspect_driver(request["driver"], request["limits"]["memory_mib"],
                                      request["limits"]["vcpus"], network_profile="loopback-ssh")
            if admitted != request["driver_admission"]:
                raise Refusal("loopback driver admission changed before launch")
        env = clean_environment(scratch, output, request["path"])
        receipt.update(status="running", driver_started=True)
        write_json(output / "inside.json", receipt)
        result = run([request["driver"], "--no-interactive", "-o", str(output)],
                     cwd=scratch, env=env, stdin=subprocess.DEVNULL, check=False)
        receipt.update(status="passed" if result.returncode == 0 else "failed", exit_code=result.returncode)
        return result.returncode
    except Exception as error:
        receipt.update(status="failed" if receipt["driver_started"] else "blocked", error=str(error)[:4096])
        return 1
    finally:
        write_json(output / "inside.json", receipt)


def monitor(process, request, receipt, sample, clock=time.monotonic, sleep=time.sleep):
    start = clock()
    limits = request["limits"]
    while process.poll() is None:
        current = sample()
        disk = min(current["disk_free_bytes"].values())
        memory = current["memory_available_bytes"]
        receipt["minimum_disk_free_bytes"] = min(receipt.get("minimum_disk_free_bytes", disk), disk)
        receipt["minimum_memory_available_bytes"] = min(receipt.get("minimum_memory_available_bytes", memory), memory)
        if disk < limits["disk_reserve_gib"] * GIB or memory < limits["memory_reserve_gib"] * GIB:
            raise Refusal("host disk or RAM reserve crossed")
        if clock() - start > limits["timeout_seconds"] + 30:
            raise Refusal("lab deadline exceeded")
        if (Path(request["run_dir"]) / "driver.log").stat().st_size > limits["log_limit_mib"] * MIB:
            raise Refusal("driver log exceeded its budget")
        sleep(1)
    return process.returncode


def finish_receipt(output, receipt, service_started):
    try:
        if (output / "inside.json").exists():
            receipt["inside"] = read_json(output / "inside.json")
            receipt["driver_started"] = receipt["inside"].get("driver_started")
            if "effective_limits" in receipt["inside"]:
                receipt["resource_limits_verified"] = True
                receipt.get("cpu_allocation", {})["selection_applied"] = True
            if receipt["status"] == "passed" and receipt["inside"].get("status") != "passed":
                raise Refusal("the owned service did not record a successful driver result")
        elif service_started:
            raise Refusal("inside receipt is absent; driver execution is unknown")
    except Exception as error:
        receipt.update(status="failed", inside_receipt_error=str(error)[:4096])
    try:
        write_json(output / "receipt.json", receipt)
    except Exception as error:
        # Preserve the actual in-memory execution result for stdout.
        receipt.update(status="failed", receipt_write_error=str(error)[:4096])


def execute(args, tools):
    uid = os.geteuid()
    if uid == 0:
        raise Refusal("run the disposable lab as a normal user, not root")
    runtime = Path(f"/run/user/{uid}")
    controller = Controller(tools, runtime)
    with Slot(runtime) as slot:
        if args.recover:
            output = Path(args.recover).resolve(strict=True)
            private_directory(output)
            request = read_json(output / "request.json")
            if request["uid"] != uid or request["run_dir"] != str(output):
                raise Refusal("recovery request belongs to a different owner or path")
            policy = request_network_policy(request)
            result = cleanup(request, slot, controller)
            result["network_policy"] = policy
            write_json(output / "recovery.json", result)
            return {"status": "recovered", "output": str(output), "cleanup": result}, 0
        if slot.active.exists() or slot.active.is_symlink():
            raise Refusal(f"unresolved lab lease; inspect {slot.active} and recover its recorded run")
        output_parent = Path(args.output_dir).resolve(strict=True)
        if not output_parent.is_dir() or output_parent.stat().st_uid != uid:
            raise Refusal("evidence parent must be an existing directory owned by the current user")
        output = Path(tempfile.mkdtemp(prefix="ryxos-lab-", dir=output_parent))
        scratch = Path(tempfile.mkdtemp(prefix="ryxos-lab-", dir="/tmp"))
        nonce = uuid.uuid4().hex
        limits = {name: getattr(args, name) for name in ("memory_mib", "qemu_overhead_mib", "vcpus", "reserve_cores",
                  "disk_reserve_gib", "memory_reserve_gib", "timeout_seconds", "log_limit_mib")}
        request = {"schema_version": 1, "uid": uid, "nonce": nonce, "unit": "ryxos-lab-" + nonce + ".service",
                   "description": "RyxOS disposable lab " + nonce, "run_dir": str(output),
                   "scratch": str(scratch), "scratch_identity": private_directory(scratch), "limits": limits,
                   "memory_max_bytes": (args.memory_mib + args.qemu_overhead_mib) * MIB, "path": tools["path"],
                   "launch_state": "not_submitted"}
        receipt = {"schema_version": 1, "status": "blocked", "driver_started": False,
                   "output": str(output), "unit": request["unit"], "nonce": nonce, "limits": limits,
                   "resource_limits_verified": False, "cleanup": {}}
        process = None
        leased = False
        previous = {}
        try:
            write_json(scratch / "ownership.json", {"nonce": nonce}, exclusive=True)
            for name in ("home", "tmp", "runtime", "config", "cache", "state"):
                (scratch / name).mkdir(mode=0o700)
            receipt["driver_admission"] = inspect_driver(args.driver, args.memory_mib, args.vcpus,
                                                         network_profile=args.network_profile or "closed")
            request["driver"] = receipt["driver_admission"]["driver"]
            request["driver_admission"] = receipt["driver_admission"]
            request["network_policy"] = receipt["driver_admission"]["network_policy"]
            receipt["network_policy"] = request["network_policy"]
            receipt["gc_root"] = retain_driver(output, receipt["driver_admission"], tools["nix_store"],
                                                clean_environment(scratch, output, tools["path"]))
            request["gc_root"] = receipt["gc_root"]
            receipt["hardware"] = hardware_admission()
            paths = {"scratch": scratch, "evidence": output, "store": Path("/nix/store")}
            receipt["initial_resources"] = resource_sample(paths)
            topology = subprocess.run([tools["lscpu"], "--json", "--extended=CPU,CORE,SOCKET,NODE,ONLINE"],
                                      env=controller.env, check=True, capture_output=True, text=True, timeout=10)
            allocation = admit(receipt["initial_resources"], limits, json.loads(topology.stdout)["cpus"], os.sched_getaffinity(0))
            receipt["cpu_allocation"] = allocation
            request["cpus"] = allocation["lab_cpus"]
            write_json(output / "request.json", request, exclusive=True)
            write_json(output / "receipt.json", receipt)
            write_json(slot.active, {"nonce": nonce, "run_dir": str(output), "unit": request["unit"]}, exclusive=True)
            leased = True
            def interrupted(signum, _frame):
                raise Refusal(f"lab interrupted by signal {signum}")
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, interrupted)
            fd = os.open(output / "driver.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as log:
                process = submit(request, tools, controller.env, log)
                receipt["driver_started"] = None
                code = monitor(process, request, receipt, lambda: resource_sample(paths))
            request["launch_state"] = "settled"
            write_json(output / "request.json", request)
            receipt.update(status="passed" if code == 0 else "failed", exit_code=code)
        except Exception as error:
            receipt.update(status="failed" if process else "blocked", error=str(error)[:4096])
        finally:
            for sig in previous:
                signal.signal(sig, signal.SIG_IGN)
            try:
                if leased:
                    receipt["cleanup"] = cleanup(request, slot, controller, process)
                else:
                    receipt["cleanup"]["scratch"] = remove_scratch(request)
            except Exception as error:
                receipt.update(status="failed", cleanup_pending=True)
                receipt["cleanup"]["error"] = str(error)[:4096]
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            finish_receipt(output, receipt, process is not None)
        return receipt, 0 if receipt["status"] == "passed" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inside", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--recover", type=Path, help="stop and clean only a recorded, unresolved owned run")
    parser.add_argument("--driver", help="realized immutable NixOS test driver executable")
    parser.add_argument("--network-profile", choices=NETWORK_PROFILES,
                        help="fixed launcher policy (default: closed); recovery uses the recorded policy")
    parser.add_argument("--output-dir", type=Path, help="existing evidence parent; a new private run directory is created")
    for name, default in (("memory-mib", 4096), ("qemu-overhead-mib", 2048), ("vcpus", 2), ("reserve-cores", 2),
                          ("disk-reserve-gib", 21), ("memory-reserve-gib", 4), ("timeout-seconds", 900), ("log-limit-mib", 64)):
        parser.add_argument("--" + name, type=int, default=default)
    for name, default in (("systemd-run", "systemd-run"), ("systemctl", "systemctl"), ("lscpu", "lscpu"),
                          ("env", "env"), ("nix-store", "nix-store")):
        parser.add_argument("--" + name, default=default, help="executable path (packaging/testing integration)")
    args = parser.parse_args(argv)
    if (args.recover or args.inside) and args.network_profile is not None:
        parser.error("inside execution and recovery use the recorded network policy; overrides are forbidden")
    if args.inside:
        return inside(args.inside)
    if not args.recover and (not args.driver or not args.output_dir):
        parser.error("--driver and --output-dir are required")
    if any(getattr(args, name) <= 0 for name in ("memory_mib", "qemu_overhead_mib", "vcpus", "reserve_cores",
           "disk_reserve_gib", "memory_reserve_gib", "timeout_seconds", "log_limit_mib")):
        parser.error("all resource budgets must be positive")
    try:
        tools = {name: executable(getattr(args, name)) for name in ("systemd_run", "systemctl", "lscpu", "env", "nix_store")}
        tools["python"] = str(Path(sys.executable).resolve(strict=True))
        tools["path"] = os.pathsep.join(dict.fromkeys(str(Path(path).parent) for path in tools.values()))
        receipt, code = execute(args, tools)
    except (OSError, ValueError, KeyError, Refusal, subprocess.SubprocessError) as error:
        receipt, code = {"status": "blocked", "error": str(error)[:4096]}, 1
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
