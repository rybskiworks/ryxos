import contextlib
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "modules" / "xen-domain.py"
MODULE_SPEC = importlib.util.spec_from_file_location("xen_domain", SOURCE)
domain = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(domain)


class FakeXl:
    def __init__(self):
        self.live = []
        self.calls = []
        self.specifications = []
        self.graceful = True
        self.create_error = False
        self.create_then_error = False
        self.config_override = {}

    def __call__(self, spec, *arguments, timeout=15):
        self.calls.append(arguments)
        self.specifications.append(spec)
        if arguments == ("list",):
            rows = ["Name ID Mem VCPUs State Time(s)", "Domain-0 0 2048 2 r----- 0.0"]
            rows.extend(f"{item['name']} {item['domid']} 512 1 r----- 0.0" for item in self.live)
            return "\n".join(rows) + "\n"
        if arguments == ("list", "--long"):
            return json.dumps([
                {"domid": item["domid"], "config": {"c_info": {key: item[key] for key in ("name", "uuid")}}}
                for item in self.live
            ])
        if arguments[:2] == ("create", "--dryrun"):
            return json.dumps({
                "c_info": {key: spec[key] for key in ("name", "uuid")},
                "on_poweroff": "destroy", "on_reboot": "destroy", "on_crash": "destroy",
            } | self.config_override)
        if arguments[0] == "create":
            receipt = json.loads((domain.state_path(spec["name"]) / "state.json").read_text())
            if receipt["phase"] != "creating" or domain.lease_target(spec["name"]) != spec["specPath"]:
                raise AssertionError("Ownership intent and GC lease must precede create")
            if self.create_error:
                raise domain.Refused("ambiguous synthetic create timeout")
            self.live = [{"domid": 42, "name": spec["name"], "uuid": spec["uuid"]}]
            if self.create_then_error:
                raise domain.Refused("synthetic timeout after Xen created the domain")
            return ""
        if arguments[0] == "shutdown":
            if self.graceful:
                self.live = []
            return ""
        if arguments[0] == "destroy":
            self.live = []
            return ""
        raise AssertionError(f"Unexpected xl operation: {arguments}")


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.store = self.directory / "store"
        self.store.mkdir(mode=0o700)
        self.state = self.directory / "state"
        self.leases = self.directory / "leases"
        for name, value in {
            "STATE_ROOT": self.state, "LEASE_ROOT": self.leases, "STORE_ROOT": self.store,
            "TRUST_ANCHOR": self.directory, "OWNER_UID": os.geteuid(),
        }.items():
            self.enterContext(patch.object(domain, name, value))
        self.tools = self.store / "tools"
        self.tools.mkdir(mode=0o755)
        for name in ("xl", "nix-store", "qemu-img", "ryxos-xen-domain-runner"):
            (self.tools / name).write_text("synthetic executable, never executed\n")
            (self.tools / name).chmod(0o755)
        self.config = self.immutable("domain.cfg", "synthetic xl config\n")
        self.backing = self.immutable("backing.raw", "synthetic backing bytes\n")
        recovery = self.immutable("recovery.json", json.dumps({"schema": 1, "domains": {}}))
        self.spec = {
            "schema": 2, "name": "test-branch", "uuid": "f3f70b01-3a87-4b5a-a77e-6238f6b78123",
            "xl": str(self.tools / "xl"), "nixStore": str(self.tools / "nix-store"),
            "qemuImg": str(self.tools / "qemu-img"), "toolDirectories": [str(self.tools)],
            "lifecycleRunner": str(self.tools / "ryxos-xen-domain-runner"), "recoveryInventory": str(recovery),
            "configFile": str(self.config), "startTimeoutSec": 1,
            "shutdownTimeoutSec": 1, "retainPaths": [], "disposableImage": None,
        }
        self.spec_path = self.immutable("spec.json", json.dumps(self.spec))
        self.name = self.spec["name"]
        self.declarations = {self.name: str(self.spec_path)}
        self.receipt = self.state / self.name / "state.json"
        self.xl = FakeXl()
        self.enterContext(patch.object(domain, "run_xl", self.xl))
        self.enterContext(patch.object(domain.subprocess, "run", side_effect=self.run_tool))
        self.enterContext(patch.object(domain, "boot_id", return_value="f3385db4-0c26-4c49-9b8f-e5f187d48130"))
        self.clock = 0
        self.enterContext(patch.object(domain.time, "monotonic", side_effect=self.tick))
        self.enterContext(patch.object(domain.time, "sleep"))
        self.tool_calls = []

    def immutable(self, name, content):
        path = self.store / name
        path.write_text(content)
        path.chmod(0o444)
        return path

    def run_tool(self, arguments, **kwargs):
        self.tool_calls.append(arguments)
        if Path(arguments[0]).name == "nix-store":
            self.assertEqual(arguments[1:4], ["--add-root", str(self.leases / self.name), "--realise"])
            self.assertEqual(kwargs["env"]["NIX_REMOTE"], "daemon")
            (self.leases / self.name).symlink_to(arguments[4])
        elif Path(arguments[0]).name == "qemu-img":
            self.assertEqual(arguments[1:7], ["create", "-f", "qcow2", "-F", "raw", "-b"])
            self.assertEqual(arguments[7], str(self.backing))
            self.assertEqual(domain.lease_target(self.name), self.declarations[self.name])
            self.assertEqual(self.saved()["phase"], "preparing")
            Path(arguments[8]).write_bytes(b"synthetic qcow2\n")
            Path(arguments[8]).chmod(0o600)
        else:
            self.fail(f"A real or unexpected tool was attempted: {arguments[0]}")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def tick(self):
        self.clock += 0.25
        return self.clock

    def saved(self):
        return json.loads(self.receipt.read_text())

    def actions(self):
        return [call for call in self.xl.calls if call[0] in ("create", "shutdown", "destroy") and "--dryrun" not in call]

    def start(self):
        domain.start(self.name, self.declarations)

    def changed(self, **changes):
        spec = self.spec | changes
        path = self.immutable("changed-spec.json", json.dumps(spec))
        return {self.name: str(path)}

    def managed(self):
        self.declarations = self.changed(disposableImage=str(self.backing))

    def test_start_stop_restart_retains_lease_and_state(self):
        self.start()
        self.assertEqual(self.saved()["phase"], "owned")
        self.assertEqual(self.saved()["domid"], 42)
        self.assertEqual(self.receipt.stat().st_mode & 0o777, 0o600)
        domain.stop(self.name, self.declarations)
        self.assertEqual(self.saved()["phase"], "stopped")
        self.assertEqual(domain.lease_target(self.name), str(self.spec_path))
        self.start()
        self.assertEqual(len([call for call in self.actions() if call[0] == "create"]), 2)
        self.assertEqual(len(self.tool_calls), 1)

    def test_matching_existing_domain_is_never_adopted(self):
        self.xl.live = [{"domid": 19, "name": self.name, "uuid": self.spec["uuid"]}]
        with self.assertRaisesRegex(domain.Refused, "adopt"):
            self.start()
        self.assertEqual(self.actions(), [])
        self.assertFalse(self.receipt.exists())
        self.assertFalse(self.leases.exists())

    def test_identity_and_restart_policy_refused_before_lease(self):
        for override in ({"c_info": {"name": "other", "uuid": self.spec["uuid"]}}, {"on_reboot": "restart"}):
            self.xl.config_override = override
            with self.assertRaises(domain.Refused):
                self.start()
        self.assertEqual(self.actions(), [])
        self.assertFalse(self.leases.exists())

    def test_undeclared_start_and_nonstore_inventory_refused(self):
        with self.assertRaisesRegex(domain.Refused, "currently declared"):
            domain.start(self.name, {})
        outside = self.directory / "inventory.json"
        outside.write_text(json.dumps({"schema": 1, "domains": self.declarations}))
        with self.assertRaisesRegex(domain.Refused, "immutable Nix store"):
            domain.load_inventory(outside)
        self.assertEqual(self.xl.calls, [])

    def test_inventory_and_cli_do_not_accept_caller_spec_override(self):
        inventory = self.immutable("inventory.json", json.dumps({"schema": 1, "domains": self.declarations}))
        self.assertEqual(domain.load_inventory(inventory), self.declarations)
        with patch.object(domain.sys, "argv", ["helper", str(inventory), "--spec", str(self.spec_path), "start", self.name]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                domain.main()
        self.assertEqual(self.xl.calls, [])

    def test_managed_overlay_is_explicit_raw_backing_and_survives_stop(self):
        self.managed()
        before = self.backing.read_bytes()
        self.start()
        disk = self.receipt.parent / "disk.qcow2"
        identity = domain.disk_identity(disk)
        self.assertEqual(self.saved()["diskIdentity"], identity)
        self.assertIn('format=qcow2,vdev=xvda,access=rw,backendtype=qdisk,target=', self.actions()[0][-1])
        domain.stop(self.name, self.declarations)
        self.start()
        self.assertEqual(domain.disk_identity(disk), identity)
        self.assertEqual(len([args for args in self.tool_calls if Path(args[0]).name == "qemu-img"]), 1)
        self.assertEqual(self.backing.read_bytes(), before)

    def test_changed_declaration_uses_retained_spec_for_stop_then_requires_reset(self):
        self.start()
        changed = self.changed(uuid="a3f70b01-3a87-4b5a-a77e-6238f6b78123", shutdownTimeoutSec=2)
        observed = domain.status(self.name, changed)
        self.assertTrue(observed["declaration_changed"])
        self.assertEqual(observed["uuid"], self.spec["uuid"])
        domain.stop(self.name, changed)
        self.assertEqual(self.xl.specifications[-1]["specPath"], str(self.spec_path))
        with self.assertRaisesRegex(domain.Refused, "reset"):
            domain.start(self.name, changed)
        domain.reset(self.name, changed)
        domain.start(self.name, changed)
        self.assertEqual(self.saved()["uuid"], "a3f70b01-3a87-4b5a-a77e-6238f6b78123")

    def test_removed_declaration_still_stops_and_retires_retained_state(self):
        self.start()
        self.assertTrue(domain.status(self.name, {})["declaration_changed"])
        domain.stop(self.name, {})
        domain.reset(self.name, {})
        self.assertFalse(self.receipt.parent.exists())
        self.assertFalse(os.path.lexists(self.leases / self.name))
        with self.assertRaisesRegex(domain.Refused, "currently declared"):
            domain.start(self.name, {})

    def test_ambiguous_create_retains_lease_and_refuses_retry_or_implicit_adoption(self):
        self.xl.create_error = True
        with self.assertRaises(domain.Refused):
            self.start()
        self.assertEqual(self.saved()["phase"], "creating")
        self.assertEqual(domain.lease_target(self.name), str(self.spec_path))
        with self.assertRaisesRegex(domain.Refused, "recovery"):
            self.start()
        with self.assertRaisesRegex(domain.Refused, "Ambiguous"):
            domain.stop(self.name, self.declarations)
        domain.reset(self.name, self.declarations)
        self.assertFalse(self.receipt.exists())

    def test_create_timeout_with_live_guest_refuses_stop_force_and_reset(self):
        self.xl.create_then_error = True
        with self.assertRaises(domain.Refused):
            self.start()
        before = self.receipt.read_bytes()
        for force in (False, True):
            with self.assertRaisesRegex(domain.Refused, "confirmed ownership"):
                domain.stop(self.name, self.declarations, force=force)
        with self.assertRaisesRegex(domain.Refused, "confirmed absence"):
            domain.reset(self.name, self.declarations)
        self.assertEqual(self.receipt.read_bytes(), before)
        self.assertFalse(domain.status(self.name, self.declarations)["owned"])
        self.assertFalse(any(call[0] in ("shutdown", "destroy") for call in self.actions()))

    def test_post_create_receipt_failure_preserves_intent_and_lease(self):
        original = domain.write_receipt
        def fail_owned(path, value):
            if value["phase"] == "owned":
                raise OSError("synthetic final receipt write failure")
            original(path, value)
        with patch.object(domain, "write_receipt", side_effect=fail_owned), self.assertRaises(OSError):
            self.start()
        self.assertEqual(self.saved()["phase"], "creating")
        self.assertTrue(self.xl.live)
        self.assertEqual(domain.lease_target(self.name), str(self.spec_path))
        with self.assertRaisesRegex(domain.Refused, "confirmed ownership"):
            domain.stop(self.name, self.declarations, force=True)

    def test_lease_before_first_receipt_write_is_recoverable(self):
        with patch.object(domain, "write_receipt", side_effect=OSError("synthetic disk full")), self.assertRaises(OSError):
            self.start()
        self.assertFalse(self.receipt.exists())
        self.assertEqual(domain.lease_target(self.name), str(self.spec_path))
        self.assertEqual(self.actions(), [])
        with self.assertRaisesRegex(domain.Refused, "retained a GC lease"):
            self.start()
        domain.reset(self.name, {})
        self.assertIsNone(domain.lease_target(self.name))

    def test_overlay_creation_failure_preserves_backing_lease_and_partial_for_reset(self):
        self.managed()
        original = self.run_tool
        def fail_disk(args, **kwargs):
            result = original(args, **kwargs)
            if Path(args[0]).name == "qemu-img":
                raise subprocess.CalledProcessError(1, args)
            return result
        with patch.object(domain.subprocess, "run", side_effect=fail_disk), self.assertRaises(domain.Refused):
            self.start()
        self.assertEqual(self.saved()["phase"], "preparing")
        self.assertTrue((self.receipt.parent / "disk.new").exists())
        self.assertIsNotNone(domain.lease_target(self.name))
        domain.reset(self.name, self.declarations)
        self.assertTrue(self.backing.exists())
        self.assertFalse(self.receipt.parent.exists())

    def test_crash_after_overlay_publication_before_receipt_can_reset_absent_only(self):
        self.managed()
        original = domain.write_receipt
        def fail_disk_record(path, value):
            if value.get("diskIdentity") is not None:
                raise OSError("synthetic crash after publication")
            original(path, value)
        with patch.object(domain, "write_receipt", side_effect=fail_disk_record), self.assertRaises(OSError):
            self.start()
        self.assertTrue((self.receipt.parent / "disk.qcow2").exists())
        self.assertIsNone(self.saved()["diskIdentity"])
        domain.reset(self.name, self.declarations)
        self.assertFalse(self.receipt.parent.exists())

    def test_crash_between_overlay_link_and_unlink_has_bounded_pair_recovery(self):
        self.managed()
        original = Path.unlink
        def fail_temporary(path, *args, **kwargs):
            if path.name == "disk.new":
                raise OSError("synthetic crash after hard-link publication")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_temporary), self.assertRaises(OSError):
            self.start()
        disk = self.receipt.parent / "disk.qcow2"
        temporary = self.receipt.parent / "disk.new"
        self.assertEqual(disk.stat().st_ino, temporary.stat().st_ino)
        self.assertEqual(disk.stat().st_nlink, 2)
        # A third alias invalidates the narrowly admitted publication pair.
        outside = self.directory / "unexpected-alias"
        os.link(disk, outside)
        with self.assertRaisesRegex(domain.Refused, "hard links"):
            domain.reset(self.name, self.declarations)
        outside.unlink()
        domain.reset(self.name, self.declarations)
        self.assertFalse(self.receipt.parent.exists())
        self.assertIsNone(domain.lease_target(self.name))

    def test_retained_uuid_cannot_be_reused_under_another_declared_name(self):
        self.start()
        domain.stop(self.name, self.declarations)
        renamed = self.spec | {"name": "renamed"}
        renamed_path = self.immutable("renamed.json", json.dumps(renamed))
        with self.assertRaisesRegex(domain.Refused, "reserves this UUID"):
            domain.start("renamed", {"renamed": str(renamed_path)})
        self.assertFalse((self.state / "renamed").exists())
        self.assertIsNotNone(domain.lease_target(self.name))

    def test_uuid_reservation_is_rechecked_after_registering_the_lease(self):
        other = self.immutable("other.json", json.dumps(self.spec | {"name": "other"}))
        original = domain.create_lease
        def reserve_concurrently(spec):
            original(spec)
            (self.leases / "other").symlink_to(other)
        with patch.object(domain, "create_lease", side_effect=reserve_concurrently):
            with self.assertRaisesRegex(domain.Refused, "reserves this UUID"):
                self.start()
        self.assertEqual(self.actions(), [])
        self.assertEqual(domain.lease_target(self.name), str(self.spec_path))
        self.assertFalse(self.receipt.exists())

    def test_concurrent_generations_cannot_admit_one_uuid_under_two_names(self):
        self.state.mkdir(mode=0o700)
        self.leases.mkdir(mode=0o700)
        other = self.immutable("other.json", json.dumps(self.spec | {"name": "other"}))
        before, after = threading.Barrier(2), threading.Barrier(2)
        def register_both(spec):
            before.wait(timeout=5)
            (self.leases / spec["name"]).symlink_to(spec["specPath"])
            after.wait(timeout=5)
        def attempt(name, declarations):
            with domain.manager_lock(name):
                try:
                    domain.start(name, declarations)
                except domain.Refused as error:
                    return str(error)
            return "unexpected admission"
        with patch.object(domain, "create_lease", side_effect=register_both), ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                pool.submit(attempt, self.name, self.declarations),
                pool.submit(attempt, "other", {"other": str(other)}),
            ]
            self.assertTrue(all("reserves this UUID" in result.result(timeout=10) for result in results))
        self.assertEqual(self.actions(), [])
        self.assertIsNotNone(domain.lease_target(self.name))
        self.assertIsNotNone(domain.lease_target("other"))

    def test_graceful_timeout_never_escalates_and_force_stop_is_separate(self):
        self.start()
        self.xl.graceful = False
        with self.assertRaisesRegex(domain.Refused, "left running"):
            domain.stop(self.name, self.declarations)
        self.assertTrue(self.xl.live)
        self.assertFalse(any(call[0] == "destroy" for call in self.actions()))
        domain.stop(self.name, self.declarations, force=True)
        self.assertIn(("destroy", "42"), self.actions())
        self.assertEqual(self.saved()["phase"], "stopped")
        self.assertIsNotNone(domain.lease_target(self.name))

    def test_identity_and_host_boot_changes_refuse_destructive_actions(self):
        self.start()
        original = dict(self.xl.live[0])
        for change in ({"domid": 43}, {"uuid": "a3f70b01-3a87-4b5a-a77e-6238f6b78123"}):
            self.xl.live[0] = original | change
            with self.assertRaises(domain.Refused):
                domain.stop(self.name, self.declarations, force=True)
        self.xl.live[0] = original
        with patch.object(domain, "boot_id", return_value="other-host-boot"), self.assertRaises(domain.Refused):
            domain.stop(self.name, self.declarations, force=True)
        self.assertFalse(any(call[0] in ("shutdown", "destroy") for call in self.actions()))

    def test_stop_without_ownership_never_touches_live_domain(self):
        self.xl.live = [{"domid": 42, "name": self.name, "uuid": self.spec["uuid"]}]
        with self.assertRaisesRegex(domain.Refused, "No ownership"):
            domain.stop(self.name, self.declarations, force=True)
        self.assertEqual(self.actions(), [])

    def test_guest_disappearance_allows_idempotent_stop_without_disk_reset(self):
        self.start()
        self.xl.live = []
        domain.stop(self.name, self.declarations)
        domain.stop(self.name, self.declarations)
        self.assertEqual(self.saved()["phase"], "stopped")
        self.assertIsNotNone(domain.lease_target(self.name))

    def test_status_is_read_only_before_start_and_for_retained_state(self):
        observed = domain.status(self.name, self.declarations)
        self.assertFalse(observed["owned"])
        self.assertFalse(self.state.exists())
        self.start()
        before = self.receipt.read_bytes(), self.receipt.stat().st_mtime_ns
        observed = domain.status(self.name, self.declarations)
        self.assertTrue(observed["owned"])
        self.assertEqual((self.receipt.read_bytes(), self.receipt.stat().st_mtime_ns), before)
        self.assertFalse((self.state / (self.name + ".lock")).exists())

    def test_reset_requires_second_absence_check_and_keeps_everything_on_race(self):
        self.managed()
        self.start()
        domain.stop(self.name, self.declarations)
        before = self.receipt.read_bytes()
        absent = []
        appeared = [{"domid": 42, "name": self.name, "uuid": self.spec["uuid"]}]
        with patch.object(domain, "read_inventory", side_effect=[absent, appeared]), self.assertRaisesRegex(domain.Refused, "appeared"):
            domain.reset(self.name, self.declarations)
        self.assertEqual(self.receipt.read_bytes(), before)
        self.assertTrue((self.receipt.parent / "disk.qcow2").exists())
        self.assertIsNotNone(domain.lease_target(self.name))

    def test_reset_deletes_only_managed_overlay_and_retires_lease_last(self):
        self.managed()
        self.start()
        domain.stop(self.name, self.declarations)
        original = Path.unlink
        deletions = []
        def observe(path, *args, **kwargs):
            deletions.append(path.name)
            if path.name in ("state.json", "disk.qcow2"):
                self.assertIsNotNone(domain.lease_target(self.name))
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", observe):
            domain.reset(self.name, self.declarations)
        self.assertEqual(deletions, ["disk.qcow2", "state.json", self.name])
        self.assertTrue(self.backing.exists())
        self.assertFalse(self.receipt.parent.exists())

    def test_reset_never_deletes_external_or_unmanaged_disks(self):
        external = self.directory / "external.raw"
        external.write_bytes(b"external disk")
        self.start()
        domain.stop(self.name, self.declarations)
        unexpected = self.receipt.parent / "disk.qcow2"
        unexpected.write_bytes(b"unmanaged data")
        unexpected.chmod(0o600)
        with self.assertRaisesRegex(domain.Refused, "unmanaged"):
            domain.reset(self.name, self.declarations)
        self.assertTrue(unexpected.exists())
        self.assertEqual(external.read_bytes(), b"external disk")
        unexpected.unlink()
        domain.reset(self.name, self.declarations)
        self.assertEqual(external.read_bytes(), b"external disk")

    def test_replaced_overlay_is_never_removed(self):
        self.managed()
        self.start()
        domain.stop(self.name, self.declarations)
        disk = self.receipt.parent / "disk.qcow2"
        replacement = self.directory / "replacement"
        replacement.write_bytes(b"replacement inode")
        replacement.chmod(0o600)
        replacement.replace(disk)
        with self.assertRaisesRegex(domain.Refused, "identity changed"):
            domain.reset(self.name, self.declarations)
        self.assertEqual(disk.read_bytes(), b"replacement inode")
        self.assertIsNotNone(domain.lease_target(self.name))

    def test_failed_reset_retains_lease_and_can_finish_after_partial_deletion(self):
        self.managed()
        self.start()
        domain.stop(self.name, self.declarations)
        original = Path.unlink
        def fail_receipt(path, *args, **kwargs):
            if path.name == "state.json":
                raise OSError("synthetic deletion interruption")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_receipt), self.assertRaises(OSError):
            domain.reset(self.name, self.declarations)
        self.assertTrue(self.receipt.exists())
        self.assertFalse((self.receipt.parent / "disk.qcow2").exists())
        self.assertIsNotNone(domain.lease_target(self.name))
        domain.reset(self.name, self.declarations)
        self.assertIsNone(domain.lease_target(self.name))

    def test_missing_or_mismatched_gc_lease_prevents_actions(self):
        self.start()
        (self.leases / self.name).unlink()
        with self.assertRaisesRegex(domain.Refused, "matching durable"):
            domain.stop(self.name, self.declarations, force=True)
        self.assertTrue(self.xl.live)
        self.assertFalse(any(call[0] in ("shutdown", "destroy") for call in self.actions()))

    def test_symlinked_state_ancestor_and_receipt_are_rejected(self):
        target = self.directory / "target"
        target.mkdir(mode=0o700)
        self.state.symlink_to(target)
        with self.assertRaisesRegex(domain.Refused, "ancestors"):
            self.start()
        self.state.unlink()
        self.start()
        before = self.receipt.read_bytes()
        self.receipt.unlink()
        other = self.directory / "other.json"
        other.write_bytes(before)
        other.chmod(0o600)
        self.receipt.symlink_to(other)
        with self.assertRaises(domain.Refused):
            domain.stop(self.name, self.declarations)
        self.assertEqual(other.read_bytes(), before)

    def test_unknown_or_hardlinked_state_refuses_reset(self):
        self.start()
        domain.stop(self.name, self.declarations)
        unknown = self.receipt.parent / "important"
        unknown.write_text("keep")
        with self.assertRaisesRegex(domain.Refused, "Unexpected"):
            domain.reset(self.name, self.declarations)
        unknown.unlink()
        outside = self.directory / "receipt-link"
        os.link(self.receipt, outside)
        with self.assertRaisesRegex(domain.Refused, "hard links"):
            domain.reset(self.name, self.declarations)
        self.assertTrue(outside.exists())

    def test_incomplete_inventory_fails_closed(self):
        replies = ["Name ID Mem VCPUs State Time(s)\nDomain-0 0 2048 2 r----- 0.0\nother 9 512 1 r----- 0.0\n", "[]"]
        with patch.object(domain, "run_xl", side_effect=replies), self.assertRaisesRegex(domain.Refused, "omitted"):
            domain.read_inventory(self.spec)

    def test_same_name_lock_refuses_concurrency_independent_names_do_not(self):
        with domain.manager_lock(self.name):
            with self.assertRaises(BlockingIOError):
                with domain.manager_lock(self.name):
                    self.fail("Concurrent same-name operation was admitted")
            with domain.manager_lock("independent"):
                pass
        self.assertEqual((self.state / (self.name + ".lock")).stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
