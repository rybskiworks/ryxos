"""Dispatch one authenticated SSH request to a fixed Xen lifecycle command."""

import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import syslog


ACCOUNT = "ryxos-xen-control"
ACTIONS = ("status", "start", "stop", "reset")
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,62}", re.ASCII)
COMMAND = re.compile(r"(status|start|stop|reset) ([A-Za-z][A-Za-z0-9_-]{0,62})", re.ASCII)
MAX_POLICY_BYTES = 65536
MAX_COMMAND_BYTES = 80
TIMEOUT_SECONDS = 900
SUDO = "/run/wrappers/bin/sudo"
CHILD_ENV = {
    "HOME": "/var/empty",
    "PATH": "/run/wrappers/bin:/run/current-system/sw/bin",
    "LANG": "C",
    "LC_ALL": "C",
}


class Refused(Exception):
    pass


def policy_from_path(value, store=Path("/nix/store")):
    """The login wrapper, never the SSH request, selects this immutable file."""
    path = Path(value)
    if not path.is_absolute() or path.parent != store:
        raise Refused("control policy must be a top-level store file")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222 or info.st_size > MAX_POLICY_BYTES:
        raise Refused("control policy must be a bounded immutable regular file")
    policy = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(policy, dict) or set(policy) != {"schema", "helper", "domains"} or policy["schema"] != 1:
        raise Refused("unsupported control policy")
    domains = policy["domains"]
    if (not isinstance(domains, list) or not domains or len(domains) > 256
            or any(not isinstance(name, str) or NAME.fullmatch(name) is None for name in domains)
            or len(domains) != len(set(domains))):
        raise Refused("invalid domain allowlist")
    helper = policy["helper"]
    if not isinstance(helper, str):
        raise Refused("invalid lifecycle helper")
    selected = Path(helper)
    if (not selected.is_absolute() or len(selected.parts) != len(store.parts) + 3
            or selected.parents[2] != store or selected.parts[-2:] != ("bin", "ryxos-xen-domain")
            or not re.fullmatch(r"[0-9a-z]{32}-[A-Za-z0-9+._?=-]+", selected.parts[-3])):
        raise Refused("lifecycle helper must be an exact store executable")
    return policy


def parse_command(command, domains):
    if not isinstance(command, str) or len(command) > MAX_COMMAND_BYTES:
        raise Refused("expected exactly: status|start|stop|reset DOMAIN")
    match = COMMAND.fullmatch(command)
    if match is None or match[2] not in domains:
        raise Refused("request is outside the configured command allowlist")
    return match[1], match[2]


def peer_address(environment):
    """Keep audit metadata bounded; never record the raw remote command."""
    fields = environment.get("SSH_CONNECTION", "").split()
    if len(fields) != 4:
        return "unknown"
    try:
        source = str(ipaddress.ip_address(fields[0]))
        ipaddress.ip_address(fields[2])
        if any(not field.isascii() or not field.isdecimal() or not 0 < int(field) < 65536
               for field in (fields[1], fields[3])):
            return "unknown"
        return source
    except ValueError:
        return "unknown"


def audit(event):
    syslog.openlog("ryxos-xen-control", syslog.LOG_PID, syslog.LOG_AUTHPRIV)
    syslog.syslog(syslog.LOG_NOTICE, json.dumps(event, sort_keys=True, separators=(",", ":")))


def dispatch(policy, environment, run=subprocess.run, record=audit, stderr=None):
    stderr = sys.stderr if stderr is None else stderr
    event = {"account": ACCOUNT, "peer": peer_address(environment)}
    try:
        action, name = parse_command(environment.get("SSH_ORIGINAL_COMMAND"), policy["domains"])
    except Refused:
        record(event | {"result": "refused", "exit_code": 64})
        print("Only status, start, stop or reset followed by one enrolled domain is permitted.", file=stderr)
        return 64

    event |= {"action": action, "domain": name}
    record(event | {"result": "started"})
    arguments = [SUDO, "-n", "-u", "root", "--", policy["helper"], action, name]
    try:
        result = run(arguments, shell=False, env=dict(CHILD_ENV), cwd="/",
                     stdin=subprocess.DEVNULL, timeout=TIMEOUT_SECONDS, check=False)
        code = result.returncode if 0 <= result.returncode <= 255 else 1
        record(event | {"result": "completed" if code == 0 else "failed", "exit_code": code})
        return code
    except subprocess.TimeoutExpired:
        # Do not infer Xen absence or issue force-stop after an interrupted tool.
        record(event | {"result": "timeout", "exit_code": 124})
        print("Lifecycle command timed out; inspect status before retrying. No forced stop was requested.", file=stderr)
        return 124
    except OSError:
        record(event | {"result": "unavailable", "exit_code": 69})
        print("The enrolled lifecycle helper could not be invoked.", file=stderr)
        return 69


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    try:
        if len(arguments) != 1:
            raise Refused("the packaged wrapper must select one policy")
        policy = policy_from_path(arguments[0])
    except (Refused, OSError, ValueError, TypeError):
        audit({"account": ACCOUNT, "result": "invalid_policy", "exit_code": 78})
        print("The fixed SSH control policy is unavailable or invalid.", file=sys.stderr)
        return 78
    return dispatch(policy, os.environ)


if __name__ == "__main__":
    raise SystemExit(main())
