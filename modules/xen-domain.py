"""Manage declared Xen identities, retained Nix closures and disposable disks."""

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


STATE_ROOT = Path("/var/lib/ryxos-xen-domains")
LEASE_ROOT = Path("/nix/var/nix/gcroots/ryxos-xen-domains")
STORE_ROOT = Path("/nix/store")
TRUST_ANCHOR = Path("/")
OWNER_UID = 0
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,62}")


class Refused(RuntimeError):
    pass


def valid_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise Refused("Invalid declared domain name")
    return name


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def secure_directory(path, *, create=False, private=True):
    """Check every ancestor; only the requested final directory may be created."""
    relative = path.relative_to(TRUST_ANCHOR)
    current = TRUST_ANCHOR
    for component in (None, *relative.parts):
        if component is not None:
            current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            if current != path or not create:
                raise
            current.mkdir(mode=0o700)
            sync_directory(current.parent)
            info = current.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != OWNER_UID
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise Refused("State and lease ancestors must be owned directories without writable peers or symlinks")
    if private and stat.S_IMODE(info.st_mode) & 0o077:
        raise Refused("State and lease directories must have mode 0700")


def regular_file(path, *, private=True, links=1):
    info = path.lstat()
    forbidden = 0o077 if private else 0o022
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != OWNER_UID
            or stat.S_IMODE(info.st_mode) & forbidden or (private and info.st_nlink != links)):
        raise Refused("Expected an owned regular file with safe permissions and no mutable hard links")
    return info


def store_path(value, *, executable=False, regular=False):
    path = Path(value)
    if not path.is_absolute() or not path.is_relative_to(STORE_ROOT) or path == STORE_ROOT:
        raise Refused("Declarations and tools must reside in the immutable Nix store")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(STORE_ROOT) or resolved == STORE_ROOT:
        raise Refused("Store reference escapes the immutable Nix store")
    info = resolved.stat()
    if info.st_uid != OWNER_UID or stat.S_IMODE(info.st_mode) & 0o022:
        raise Refused("Store reference is not immutable and owned")
    if regular and not stat.S_ISREG(info.st_mode):
        raise Refused("Declared store file must be a regular file")
    if executable and (not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111):
        raise Refused("Declared tool must be an executable store file")
    return path


def read_json(path, *, private=True):
    info = regular_file(path, private=private)
    if info.st_size > 1024 * 1024:
        raise Refused("Lifecycle metadata exceeds its size limit")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise Refused("Metadata changed during admission")
        value = json.load(stream)
    if not isinstance(value, dict):
        raise Refused("Lifecycle metadata must be a JSON object")
    return value


def load_inventory(path):
    inventory = read_json(store_path(path, regular=True), private=False)
    if inventory.get("schema") != 1 or not isinstance(inventory.get("domains"), dict):
        raise Refused("Unsupported declared inventory")
    return {valid_name(name): str(store_path(spec, regular=True))
            for name, spec in inventory["domains"].items()}


def load_spec(path, name):
    path = store_path(path, regular=True)
    spec = read_json(path, private=False)
    if spec.get("schema") != 2 or spec.get("name") != valid_name(name):
        raise Refused("Retained specification does not match its domain name or schema")
    if str(uuid.UUID(spec["uuid"])) != spec["uuid"] or uuid.UUID(spec["uuid"]).int == 0:
        raise Refused("Invalid domain UUID")
    for key in ("startTimeoutSec", "shutdownTimeoutSec"):
        if type(spec[key]) is not int or not 1 <= spec[key] <= 600:
            raise Refused("Timeout must be an integer between 1 and 600 seconds")
    store_path(spec["configFile"], regular=True)
    for key in ("xl", "nixStore", "qemuImg", "lifecycleRunner"):
        store_path(spec[key], executable=True)
    store_path(spec["recoveryInventory"], regular=True)
    if not isinstance(spec["toolDirectories"], list) or not spec["toolDirectories"]:
        raise Refused("A pinned tool search path is required")
    for value in spec["toolDirectories"]:
        if not store_path(value).is_dir():
            raise Refused("Tool search path must contain store directories")
    for value in spec["retainPaths"]:
        store_path(value)
    if spec["disposableImage"] is not None:
        store_path(spec["disposableImage"], regular=True)
    return spec | {"specPath": str(path)}


def tool_environment(spec):
    return {"PATH": os.pathsep.join(spec["toolDirectories"]), "LC_ALL": "C"}


def run_xl(spec, *arguments, timeout=15):
    try:
        completed = subprocess.run(
            [spec["xl"], *arguments], check=True, capture_output=True,
            text=True, timeout=timeout, env=tool_environment(spec),
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
        if len(fields) < 2 or not fields[1].isdigit() or int(fields[1]) in ids:
            raise Refused("Malformed or duplicate xl list domain row")
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
        domains.append({"domid": domid, "name": info["name"], "uuid": str(uuid.UUID(info["uuid"]))})
    observed_ids = {domain["domid"] for domain in domains}
    # xl can omit domains whose saved configuration cannot be retrieved.
    if observed_ids != ids - {0} or len(observed_ids) != len(domains):
        raise Refused("Xen inventory changed or omitted domain metadata; retry after inspection")
    return domains


def matching_domain(spec, domains):
    matches = [item for item in domains if item["name"] == spec["name"] or item["uuid"] == spec["uuid"]]
    if not matches:
        return None
    if len(matches) != 1 or any(matches[0][key] != spec[key] for key in ("name", "uuid")):
        raise Refused("An existing domain conflicts with the configured name or UUID")
    return matches[0]


def boot_id():
    return str(uuid.UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()))


def state_path(name):
    return STATE_ROOT / valid_name(name)


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
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def lease_target(name):
    if not os.path.lexists(LEASE_ROOT):
        return None
    secure_directory(LEASE_ROOT)
    path = LEASE_ROOT / valid_name(name)
    if not os.path.lexists(path):
        return None
    info = path.lstat()
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != OWNER_UID:
        raise Refused("GC lease must be an owned store symlink")
    return str(store_path(os.readlink(path), regular=True))


def create_lease(spec):
    secure_directory(LEASE_ROOT, create=True)
    if lease_target(spec["name"]) is not None:
        raise Refused("A retained GC lease already exists; inspect and reset before start")
    # The spec is already realised. A direct root keeps its referenced config,
    # tools, boot artifacts and backing image alive independently of generations.
    try:
        subprocess.run(
            [spec["nixStore"], "--add-root", str(LEASE_ROOT / spec["name"]), "--realise", spec["specPath"]],
            check=True, capture_output=True, text=True, timeout=30,
            env=tool_environment(spec) | {
                "NIX_REMOTE": "daemon", "NIX_USER_CONF_FILES": "/dev/null",
                "NIX_CONFIG": "substitute = false\nbuilders =\n",
            },
        )
    except subprocess.SubprocessError as error:
        raise Refused("Could not retain the immutable specification; inspect its GC lease") from error
    if lease_target(spec["name"]) != spec["specPath"]:
        raise Refused("GC lease did not retain the requested immutable specification")
    sync_directory(LEASE_ROOT)


def check_reserved_uuid(spec):
    if not os.path.lexists(LEASE_ROOT):
        return
    secure_directory(LEASE_ROOT)
    for path in LEASE_ROOT.iterdir():
        if path.name == spec["name"]:
            continue
        target = lease_target(valid_name(path.name))
        if target is None:
            continue
        retained = load_spec(target, path.name)
        if retained["uuid"] == spec["uuid"]:
            raise Refused("Another retained domain reserves this UUID; reset its absent state first")


def active_state(name):
    directory = state_path(name)
    if not os.path.lexists(STATE_ROOT):
        return None, None
    secure_directory(STATE_ROOT)
    if not os.path.lexists(directory):
        return None, None
    secure_directory(directory)
    path = directory / "state.json"
    if not os.path.lexists(path):
        return None, None
    receipt = read_json(path)
    if receipt.get("schema") != 2 or receipt.get("phase") not in ("preparing", "creating", "owned", "stopped"):
        raise Refused("Unsupported ownership receipt; inspect before migration")
    spec = load_spec(receipt["specPath"], name)
    if any(receipt.get(key) != spec[key] for key in ("name", "uuid")):
        raise Refused("Ownership receipt and retained specification disagree")
    if receipt["phase"] == "owned" and (type(receipt.get("domid")) is not int or receipt["domid"] <= 0):
        raise Refused("Owned receipt has no valid domain ID")
    if lease_target(name) != spec["specPath"]:
        raise Refused("Ownership receipt has no matching durable GC lease")
    return spec, receipt


def disk_identity(path):
    info = regular_file(path)
    return {"device": info.st_dev, "inode": info.st_ino}


def validate_disk(spec, receipt, *, resetting=False):
    path = state_path(spec["name"]) / "disk.qcow2"
    present = os.path.lexists(path)
    if spec["disposableImage"] is None:
        if present or receipt.get("diskIdentity") is not None:
            raise Refused("Unmanaged disk data must not enter disposable state")
        return
    if not present:
        if resetting or receipt["phase"] == "preparing":
            return
        raise Refused("Managed disposable disk is missing; explicit reset is required")
    identity = disk_identity(path)
    # A crash can occur between atomic publication and the next receipt write.
    # Only reset of an absent, preparing instance may retire that fixed file.
    if resetting and receipt["phase"] == "preparing" and receipt.get("diskIdentity") is None:
        return
    if identity != receipt.get("diskIdentity"):
        raise Refused("Disposable disk identity changed; no disk action is safe")


def config_arguments(spec):
    arguments = [spec["configFile"]]
    if spec["disposableImage"] is not None:
        disk = state_path(spec["name"]) / "disk.qcow2"
        arguments.append("disk=" + json.dumps([
            f"format=qcow2,vdev=xvda,access=rw,backendtype=qdisk,target={disk}",
        ]))
    return arguments


def validate_config(spec):
    parsed = json.loads(run_xl(spec, "create", "--dryrun", *config_arguments(spec)))
    info = parsed.get("c_info", {})
    if any(info.get(key) != spec[key] for key in ("name", "uuid")):
        raise Refused("xl configuration name/UUID do not match their declared identity")
    for action in ("on_poweroff", "on_reboot", "on_crash"):
        if parsed.get(action) != "destroy":
            raise Refused(f"Managed xl configuration must explicitly use {action}=destroy")


def prepare_disk(spec):
    if spec["disposableImage"] is None:
        return None
    directory = state_path(spec["name"])
    temporary, destination = directory / "disk.new", directory / "disk.qcow2"
    if os.path.lexists(temporary) or os.path.lexists(destination):
        raise Refused("Disposable disk already exists; explicit reset is required")
    try:
        subprocess.run(
            [spec["qemuImg"], "create", "-f", "qcow2", "-F", "raw", "-b", spec["disposableImage"], str(temporary)],
            check=True, capture_output=True, text=True, timeout=60, env=tool_environment(spec),
        )
    except subprocess.SubprocessError as error:
        raise Refused("Disposable disk creation failed; state and GC lease retained") from error
    regular_file(temporary)
    descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.link(temporary, destination, follow_symlinks=False)
    temporary.unlink()
    sync_directory(directory)
    return disk_identity(destination)


def start(name, declarations):
    if name not in declarations:
        raise Refused("Only a currently declared domain may be started")
    spec = load_spec(declarations[name], name)
    retained, receipt = active_state(name)
    if receipt is not None:
        if receipt["phase"] != "stopped" or retained["specPath"] != spec["specPath"]:
            raise Refused("Retained state requires explicit stop/recovery and reset before this start")
        validate_disk(spec, receipt)
    elif lease_target(name) is not None:
        raise Refused("An incomplete start retained a GC lease; inspect and reset before retry")
    check_reserved_uuid(spec)
    if matching_domain(spec, read_inventory(spec)) is not None:
        raise Refused("Refusing to adopt an existing domain, even when its UUID matches")
    validate_config(spec)
    directory = state_path(name)
    secure_directory(STATE_ROOT, create=True)
    secure_directory(directory, create=True)
    if receipt is None:
        if any(directory.iterdir()):
            raise Refused("Unexpected state files require inspection before start")
        create_lease(spec)
        # Reserve before checking again: simultaneous old/new inventories using
        # the same UUID under different names cannot both pass admission.
        check_reserved_uuid(spec)
        receipt = {
            "schema": 2, "name": name, "uuid": spec["uuid"], "specPath": spec["specPath"],
            "phase": "preparing", "hostBootId": boot_id(), "diskIdentity": None,
        }
        write_receipt(directory / "state.json", receipt)
        receipt["diskIdentity"] = prepare_disk(spec)
        write_receipt(directory / "state.json", receipt)
    if matching_domain(spec, read_inventory(spec)) is not None:
        raise Refused("Domain appeared during admission; no create was issued")
    receipt.update(phase="creating", hostBootId=boot_id(), domid=None)
    write_receipt(directory / "state.json", receipt)
    run_xl(spec, "create", *config_arguments(spec), timeout=spec["startTimeoutSec"])
    live = matching_domain(spec, read_inventory(spec))
    if live is None:
        raise Refused("Create returned but the domain is absent; ownership and GC lease retained")
    receipt.update(live)
    receipt["phase"] = "owned"
    write_receipt(directory / "state.json", receipt)


def owned_live(spec, receipt):
    live = matching_domain(spec, read_inventory(spec))
    if live is not None:
        if receipt["phase"] != "owned" or receipt.get("hostBootId") != boot_id():
            raise Refused("Live domain has no confirmed ownership in this host boot; inspect externally")
        if live["domid"] != receipt["domid"]:
            raise Refused("Domain ID changed since creation; refusing lifecycle action")
    return live


def selected_state(name, declarations):
    spec, receipt = active_state(name)
    if spec is None:
        path = lease_target(name) or declarations.get(name)
        if path is None:
            raise Refused("Unknown domain: no declaration or retained specification")
        spec = load_spec(path, name)
    return spec, receipt


def status(name, declarations):
    spec, receipt = selected_state(name, declarations)
    live = matching_domain(spec, read_inventory(spec))
    owned = receipt is not None and receipt["phase"] == "owned"
    if owned and live is not None:
        if receipt.get("hostBootId") != boot_id() or receipt["domid"] != live["domid"]:
            raise Refused("Live domain ID or host boot changed; refusing ownership status")
    return {
        "action": "status", "name": name, "uuid": spec["uuid"],
        "status": "present" if live else "absent", "domid": live["domid"] if live else None,
        "receipt_phase": receipt["phase"] if receipt else None, "owned": bool(live and owned),
        "retained_spec": lease_target(name), "declared_spec": declarations.get(name),
        "declaration_changed": declarations.get(name) != spec["specPath"],
    }


def stop(name, declarations, *, force=False):
    spec, receipt = selected_state(name, declarations)
    if receipt is None:
        if matching_domain(spec, read_inventory(spec)) is not None:
            raise Refused("No ownership receipt; refusing to stop an existing domain")
        return
    live = owned_live(spec, receipt)
    if live is None and receipt["phase"] in ("preparing", "creating"):
        raise Refused("Ambiguous start requires inspection and absent-only reset")
    if live is not None:
        run_xl(spec, "destroy" if force else "shutdown", str(live["domid"]))
        deadline = time.monotonic() + spec["shutdownTimeoutSec"]
        while owned_live(spec, receipt) is not None:
            if time.monotonic() >= deadline:
                raise Refused("Shutdown timed out; domain left running and state/GC lease retained")
            time.sleep(0.25)
    receipt["phase"] = "stopped"
    write_receipt(state_path(name) / "state.json", receipt)


def reset(name, declarations):
    spec, receipt = selected_state(name, declarations)
    if matching_domain(spec, read_inventory(spec)) is not None:
        raise Refused("Reset requires confirmed absence of the name and UUID")
    directory = state_path(name)
    files = []
    if os.path.lexists(directory):
        secure_directory(directory)
        files = list(directory.iterdir())
        allowed = {"state.json", "state.tmp", "disk.qcow2", "disk.new"}
        if any(path.name not in allowed for path in files):
            raise Refused("Unexpected state files prevent reset")
        linked_pair = False
        disk, temporary = directory / "disk.qcow2", directory / "disk.new"
        if (receipt is not None and receipt["phase"] == "preparing"
                and receipt.get("diskIdentity") is None and spec["disposableImage"] is not None
                and disk in files and temporary in files):
            first, second = disk.lstat(), temporary.lstat()
            linked_pair = ((first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)
                           and first.st_nlink == second.st_nlink == 2)
        for path in files:
            # The sole permitted mutable hard-link pair is interrupted atomic
            # overlay publication. Exactly two links rules out outside aliases.
            regular_file(path, links=2 if linked_pair and path in (disk, temporary) else 1)
        if spec["disposableImage"] is None and any(path.name.startswith("disk.") for path in files):
            raise Refused("Reset never deletes unmanaged disks")
        if receipt is not None and not linked_pair:
            validate_disk(spec, receipt, resetting=True)
        elif receipt is None and any(path.name.startswith("disk.") for path in files):
            raise Refused("Disk state without a receipt requires external inspection")
    time.sleep(0.25)
    # A second complete plain/long inventory is required immediately before
    # deletion. External root xl tools must respect these reserved identities.
    if matching_domain(spec, read_inventory(spec)) is not None:
        raise Refused("Domain appeared during reset; all state and GC leases retained")
    for path in sorted(files, key=lambda item: item.name == "state.json"):
        path.unlink()
    if os.path.lexists(directory):
        sync_directory(directory)
        directory.rmdir()
        sync_directory(STATE_ROOT)
    target = lease_target(name)
    if target is not None:
        if target != spec["specPath"]:
            raise Refused("GC lease changed during reset")
        (LEASE_ROOT / name).unlink()
        sync_directory(LEASE_ROOT)


@contextlib.contextmanager
def manager_lock(name):
    secure_directory(STATE_ROOT, create=True)
    path = STATE_ROOT / (valid_name(name) + ".lock")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = regular_file(path)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise Refused("Lifecycle lock changed during admission")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    # The packaged entry point supplies this immutable path before user arguments.
    parser.add_argument("inventory", type=Path)
    parser.add_argument("action", choices=("list", "start", "status", "stop", "force-stop", "reset"))
    parser.add_argument("name", nargs="?")
    args = parser.parse_args()
    def interrupted(_signum, _frame):
        raise Refused("Interrupted; incomplete operations retain their state and GC lease")
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, interrupted)
    try:
        declarations = load_inventory(args.inventory)
        if args.action == "list":
            if args.name is not None:
                raise Refused("list does not accept a domain name")
            print(json.dumps({"declared": sorted(declarations)}))
            return 0
        name = valid_name(args.name)
        if os.geteuid() != OWNER_UID:
            raise Refused("Lifecycle state inspection and changes require root")
        if args.action == "status":
            print(json.dumps(status(name, declarations), sort_keys=True))
            return 0
        os.umask(0o077)
        with manager_lock(name):
            if args.action == "start":
                start(name, declarations)
            elif args.action == "reset":
                reset(name, declarations)
            else:
                stop(name, declarations, force=args.action == "force-stop")
        print(json.dumps({"action": args.action, "name": name, "status": "completed"}))
        return 0
    except (Refused, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Xen lifecycle refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
