"""Bounded xl lifecycle with persistent, checked domain ownership."""

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
import uuid


class Refused(RuntimeError):
    pass


def run_xl(spec, *arguments, timeout=15):
    try:
        completed = subprocess.run(
            [spec["xl"], *arguments], check=True, capture_output=True,
            text=True, timeout=timeout, env={"PATH": "/run/current-system/sw/bin", "LC_ALL": "C"},
        )
    except subprocess.SubprocessError as error:
        raise Refused(f"xl {arguments[0]} failed or timed out; inspect the Xen journal") from error
    return completed.stdout


def read_inventory(spec):
    rows = run_xl(spec, "list").splitlines()
    if not rows or not rows[0].startswith("Name"):
        raise Refused("Unexpected xl list format")
    ids = set()
    for row in rows[1:]:
        fields = row.split()
        if len(fields) < 2 or not fields[1].isdigit():
            raise Refused("Malformed xl list domain row")
        ids.add(int(fields[1]))
    details = json.loads(run_xl(spec, "list", "--long"))
    if not isinstance(details, list):
        raise Refused("xl list --long must use JSON output")
    domains = []
    for item in details:
        domid = item.get("domid")
        if domid == 0:
            continue
        info = item.get("config", {}).get("c_info", {})
        if type(domid) is not int or domid <= 0 or not isinstance(info.get("name"), str):
            raise Refused("Incomplete Xen domain identity")
        domain_uuid = str(uuid.UUID(info["uuid"]))
        domains.append({"domid": domid, "name": info["name"], "uuid": domain_uuid})
    # xl can omit domains whose saved configuration cannot be retrieved.
    if {domain["domid"] for domain in domains} != ids - {0}:
        raise Refused("Xen inventory changed or omitted domain metadata; retry after inspection")
    return domains


def matching_domain(spec, domains):
    matches = [item for item in domains if item["name"] == spec["name"] or item["uuid"] == spec["uuid"]]
    if not matches:
        return None
    if len(matches) != 1 or any(matches[0][key] != spec[key] for key in ("name", "uuid")):
        raise Refused("An existing domain conflicts with the configured name or UUID")
    return matches[0]


def write_receipt(path, value):
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def load_receipt(path, spec):
    if not path.exists():
        return None
    if not stat.S_ISREG(path.lstat().st_mode) or path.is_symlink():
        raise Refused("Ownership receipt must be a regular file")
    receipt = json.loads(path.read_text())
    if any(receipt.get(key) != spec[key] for key in ("name", "uuid")):
        raise Refused("Ownership receipt belongs to a different domain configuration")
    if receipt.get("schema") != 1 or receipt.get("phase") not in ("creating", "owned"):
        raise Refused("Ownership receipt has an unsupported schema or phase")
    if receipt["phase"] == "owned" and (type(receipt.get("domid")) is not int or receipt["domid"] <= 0):
        raise Refused("Owned receipt has no valid guest domain ID")
    return receipt


def validate_config(spec):
    parsed = json.loads(run_xl(spec, "create", "--dryrun", spec["configFile"]))
    info = parsed.get("c_info", {})
    if any(info.get(key) != spec[key] for key in ("name", "uuid")):
        raise Refused("xl configuration name/UUID do not match their declared identity")
    for action in ("on_poweroff", "on_reboot", "on_crash"):
        if parsed.get(action) != "destroy":
            raise Refused(f"Managed xl configuration must explicitly use {action}=destroy")


def start(spec, path):
    if load_receipt(path, spec) is not None:
        raise Refused("Existing ownership receipt requires an explicit stop/recovery before start")
    if matching_domain(spec, read_inventory(spec)) is not None:
        raise Refused("Refusing to adopt an existing domain, even when its UUID matches")
    validate_config(spec)
    if matching_domain(spec, read_inventory(spec)) is not None:
        raise Refused("Domain appeared during admission; no create was issued")
    receipt = {"schema": 1, "name": spec["name"], "uuid": spec["uuid"], "phase": "creating"}
    write_receipt(path, receipt)
    # An ambiguous create failure retains this receipt. No automatic retry or
    # destruction is safe until a live UUID can be verified.
    run_xl(spec, "create", spec["configFile"], timeout=spec["startTimeoutSec"])
    live = matching_domain(spec, read_inventory(spec))
    if live is None:
        raise Refused("Create returned but the domain is absent; ownership receipt retained")
    receipt.update(live)
    receipt["phase"] = "owned"
    write_receipt(path, receipt)


def owned_live(spec, receipt):
    live = matching_domain(spec, read_inventory(spec))
    if live is not None and receipt.get("domid") is not None and live["domid"] != receipt["domid"]:
        raise Refused("Domain ID changed since creation; refusing lifecycle action")
    return live


def stop(spec, path):
    receipt = load_receipt(path, spec)
    live = matching_domain(spec, read_inventory(spec))
    if receipt is None:
        if live is not None:
            raise Refused("No ownership receipt; refusing to stop an existing domain")
        return
    if live is None:
        if receipt.get("phase") == "creating":
            raise Refused("Ambiguous create has no visible domain; inspect before manually retiring its receipt")
        path.unlink()
        return
    live = owned_live(spec, receipt)
    if live is None:
        raise Refused("Domain disappeared during ownership verification; retry stop after inspection")
    receipt.update(live)
    receipt["phase"] = "owned"
    write_receipt(path, receipt)
    run_xl(spec, "shutdown", str(live["domid"]))
    deadline = time.monotonic() + spec["shutdownTimeoutSec"]
    while time.monotonic() < deadline:
        if owned_live(spec, receipt) is None:
            path.unlink()
            return
        time.sleep(0.25)
    if not spec["destroyOnTimeout"]:
        raise Refused("Graceful shutdown timed out; domain left running and receipt retained")
    live = owned_live(spec, receipt)
    if live is not None:
        run_xl(spec, "destroy", str(live["domid"]))
    if owned_live(spec, receipt) is not None:
        raise Refused("Destroy returned but the owned domain still exists")
    path.unlink()


@contextlib.contextmanager
def manager_lock(directory):
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise Refused("State directory must be a private directory owned by the service user")
    descriptor = os.open(directory / "manager.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        # Fail promptly instead of consuming the entire systemd start timeout.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("action", choices=("start", "stop"))
    args = parser.parse_args()
    def interrupted(_signum, _frame):
        raise Refused("Interrupted; any incomplete creation retains its ownership receipt")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        spec = json.loads(args.spec.read_text())
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,62}", spec["name"]):
            raise Refused("Invalid domain name")
        if str(uuid.UUID(spec["uuid"])) != spec["uuid"] or uuid.UUID(spec["uuid"]).int == 0:
            raise Refused("Invalid domain UUID")
        if any(not Path(spec[key]).is_absolute() for key in ("xl", "configFile", "stateDirectory")):
            raise Refused("Executable, configuration and state paths must be absolute")
        for key in ("startTimeoutSec", "shutdownTimeoutSec"):
            if type(spec[key]) is not int or not 1 <= spec[key] <= 600:
                raise Refused("Timeout must be an integer between 1 and 600 seconds")
        if type(spec["destroyOnTimeout"]) is not bool:
            raise Refused("Destroy policy must be explicit boolean")
        directory = Path(spec["stateDirectory"])
        with manager_lock(directory):
            action = start if args.action == "start" else stop
            action(spec, directory / (spec["name"] + ".json"))
        print(json.dumps({"action": args.action, "name": spec["name"], "status": "completed"}))
        return 0
    except (Refused, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Xen lifecycle refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
