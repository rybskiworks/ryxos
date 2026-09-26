"""Fixed synthetic SSH transport for the finite Xen test; no command-line entry point."""

import json
import os
from pathlib import Path
import shlex
import socket
import stat
import subprocess
import tempfile
import threading


PORT = 22222
TARGET = "ryxos-xen-control@127.0.0.1"
NETDEV = "user,id=ssh0,restrict=on,ipv6=off,hostfwd=tcp:127.0.0.1:22222-10.0.2.15:22222"
DEVICE = "virtio-net-pci,netdev=ssh0,mac=52:54:00:72:78:01"


def listener_rows(proc=Path("/proc")):
    result = []
    for family in ("tcp", "tcp6"):
        for line in (proc / "net" / family).read_text().splitlines()[1:]:
            fields = line.split()
            address, port = fields[1].split(":")
            if fields[3] == "0A" and int(port, 16) == PORT:
                result.append({"family": family, "address": address, "inode": fields[9]})
    return result


def listener_owner(pid, proc=Path("/proc")):
    rows = listener_rows(proc)
    if len(rows) != 1 or rows[0]["family"] != "tcp" or rows[0]["address"] != "0100007F":
        raise AssertionError(f"Expected exactly one IPv4 loopback SSH listener: {rows}")
    task = proc / str(pid)
    before = (task / "stat").read_text().rsplit(")", 1)[1].split()[19]
    sockets = set()
    for fd in (task / "fd").iterdir():
        try:
            sockets.add(os.readlink(fd))
        except FileNotFoundError:
            continue
    if "socket:[" + rows[0]["inode"] + "]" not in sockets:
        raise AssertionError("The recorded QEMU process does not own the forward listener")
    after = (task / "stat").read_text().rsplit(")", 1)[1].split()[19]
    if before != after:
        raise AssertionError("QEMU process identity changed while checking its listener")
    if (task / "cgroup").read_text() != (proc / "self" / "cgroup").read_text():
        raise AssertionError("QEMU and the test driver must share the supervised cgroup")
    return {"address": "127.0.0.1", "port": PORT, "qemu_pid": pid,
            "qemu_start_ticks": before, "same_cgroup": True}


def fixture_file(value, executable=False):
    path = Path(value).resolve(strict=True)
    path.relative_to("/nix/store")
    info = path.stat()
    limit = 16 * 1024 * 1024 if executable else 1024 * 1024
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222 or info.st_size > limit:
        raise AssertionError("Expected a bounded immutable fixture file")
    if executable and not os.access(path, os.X_OK):
        raise AssertionError("Pinned SSH is not executable")
    return path


class HostTransport:
    def __init__(self, ssh, operator_key, wrong_key, host_public_key, evidence_directory):
        self.ssh = str(fixture_file(ssh, executable=True))
        operator = fixture_file(operator_key).read_bytes()
        wrong = fixture_file(wrong_key).read_bytes()
        if "\n" in host_public_key.strip() or not host_public_key.startswith(("ssh-rsa ", "ecdsa-sha2-nistp256 ")):
            raise AssertionError("Expected the pinned synthetic host public key")
        evidence = Path(evidence_directory)
        info = evidence.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise AssertionError("SSH client material requires an owned private evidence directory")
        self.temporary = tempfile.TemporaryDirectory(prefix="ssh-client-", dir=evidence)
        self.directory = Path(self.temporary.name)
        self.directory.chmod(0o700)
        self.operator_key = self.directory / "operator_ed25519"
        self.wrong_key = self.directory / "unenrolled_rsa"
        self.known_hosts = self.directory / "known_hosts"
        for path, data in ((self.operator_key, operator), (self.wrong_key, wrong),
                           (self.known_hosts, (f"[127.0.0.1]:{PORT} " + host_public_key.strip() + "\n").encode())):
            with path.open("xb") as output:
                path.chmod(0o600)
                output.write(data)
        self.environment = {"HOME": str(self.directory), "TMPDIR": str(self.directory),
                            "LANG": "C", "LC_ALL": "C", "PATH": "/no-ambient-tools"}
        self.owner = None

    def preflight(self):
        if listener_rows():
            raise AssertionError("Fixed SSH port is occupied; its owner will not be touched")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", PORT))

    def verify_listener(self, pid):
        self.owner = listener_owner(pid)
        return self.owner

    def call(self, remote=None, options=None, timeout=20, key=None, log_level="ERROR"):
        if type(timeout) is not int or not 1 <= timeout <= 180:
            raise AssertionError("SSH timeout is outside this fixture's finite budget")
        if log_level not in ("ERROR", "DEBUG1"):
            raise AssertionError("Only the fixture's normal or forwarding diagnostic verbosity is admitted")
        selected_key = self.operator_key if key is None else Path(key)
        if selected_key not in (self.operator_key, self.wrong_key):
            raise AssertionError("Only the two synthetic fixture keys are admitted")
        argv = [self.ssh, "-F", "/dev/null", "-p", str(PORT),
                "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(self.known_hosts),
                "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5",
                "-o", "ConnectionAttempts=1", "-o", "LogLevel=" + log_level, "-i", str(selected_key)]
        argv += ["-T"] if options is None else options
        argv.append(TARGET)
        if remote is not None:
            argv.append(remote)
        # Remote negative-test strings remain one SSH argument. Never a host shell.
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=self.environment,
                                timeout=timeout, check=False)
        if len(result.stdout) > 131072:
            raise AssertionError("Synthetic SSH response exceeded the bounded receipt budget")
        return result.returncode, result.stdout

    def prove_host_egress_denied(self, machine):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        stop = threading.Event()
        accepted = []
        errors = []
        thread = None
        nonce = os.urandom(16).hex().encode()
        try:
            server.bind(("127.0.0.1", 0))
            server.listen(2)
            server.settimeout(0.2)
            port = server.getsockname()[1]

            def serve():
                while not stop.is_set():
                    try:
                        connection, address = server.accept()
                    except TimeoutError:
                        continue
                    except OSError as error:
                        if not stop.is_set():
                            errors.append(str(error))
                        return
                    with connection:
                        accepted.append(address)
                        connection.settimeout(1)
                        try:
                            connection.sendall(nonce)
                        except OSError as error:
                            errors.append(str(error))

            thread = threading.Thread(target=serve, name="owned-egress-probe", daemon=True)
            thread.start()
            with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
                assert client.recv(128) == nonce, "Owned host fixture failed its positive control"
            assert len(accepted) == 1 and not errors
            code = ("import json,socket,sys\n"
                    "try:\n"
                    f" s=socket.create_connection(('10.0.2.2',{port}),timeout=3)\n"
                    "except OSError as error:\n"
                    " print(json.dumps({'denied':True,'type':type(error).__name__}));sys.exit(0)\n"
                    "else:\n s.close();print('UNEXPECTED_HOST_ACCESS');sys.exit(1)\n")
            result, output = machine.execute("python3 -c " + shlex.quote(code), timeout=8)
            assert result == 0 and json.loads(output)["denied"], (result, output)
            assert len(accepted) == 1 and not errors, (accepted, errors)
            return {"status": "passed", "positive_host_control": True,
                    "unforwarded_host_connection": "denied", "accepted_guest_connections": 0,
                    "outside_route_native_proof": False}
        finally:
            stop.set()
            server.close()
            if thread is not None:
                thread.join(timeout=2)
                if thread.is_alive():
                    raise AssertionError("Owned egress probe thread did not stop")

    def finish_receipt(self, receipt):
        ssh = receipt.get("ssh_control")
        if ssh is not None:
            ssh["operator_host_route_qualified"] = False
        try:
            cleanup = self.finish()
            receipt["host_transport_cleanup"] = cleanup
        except BaseException as error:
            receipt["status"] = "failed"
            receipt["host_transport_cleanup"] = {"error": str(error)[-4096:]}
        else:
            if ssh is not None:
                ssh["operator_host_route_qualified"] = (
                    receipt.get("status") == "passed"
                    and receipt.get("graceful_cleanup") is True
                    and receipt.get("dom0_shutdown") is True
                    and ssh.get("status") == "passed"
                    and ssh.get("host_network_connection") is True
                    and ssh.get("listener_stopped") is True
                    and cleanup.get("host_listener_gone") is True
                    and cleanup.get("fixture_keys_removed") is True
                )

    def finish(self):
        try:
            remaining = listener_rows()
            if remaining:
                raise AssertionError(f"The fixed SSH listener remains after VM shutdown: {remaining}")
            return {"host_listener_gone": True, "fixture_keys_removed": True}
        finally:
            self.temporary.cleanup()
