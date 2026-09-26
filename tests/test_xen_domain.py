import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "modules" / "xen-domain.py"
MODULE_SPEC = importlib.util.spec_from_file_location("xen_domain", SOURCE)
domain = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(domain)


class FakeXl:
    def __init__(self, spec, receipt):
        self.spec = spec
        self.receipt = receipt
        self.live = []
        self.calls = []
        self.graceful = True
        self.create_error = False
        self.config = {
            "c_info": {key: spec[key] for key in ("name", "uuid")},
            "on_poweroff": "destroy", "on_reboot": "destroy", "on_crash": "destroy",
        }

    def __call__(self, spec, *arguments, timeout=15):
        self.calls.append(arguments)
        if arguments == ("list",):
            rows = ["Name ID Mem VCPUs State Time(s)", "Domain-0 0 2048 2 r----- 0.0"]
            rows.extend(f"{item['name']} {item['domid']} 1024 1 r----- 0.0" for item in self.live)
            return "\n".join(rows) + "\n"
        if arguments == ("list", "--long"):
            return json.dumps([
                {"domid": item["domid"], "config": {"c_info": {key: item[key] for key in ("name", "uuid")}}}
                for item in self.live
            ])
        if arguments[:2] == ("create", "--dryrun"):
            return json.dumps(self.config)
        if arguments[0] == "create":
            if json.loads(self.receipt.read_text())["phase"] != "creating":
                raise AssertionError("Intent was not persisted before create")
            if self.create_error:
                raise domain.Refused("ambiguous synthetic create timeout")
            self.live = [{"domid": 42, "name": spec["name"], "uuid": spec["uuid"]}]
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
        self.spec = {
            "name": "test-branch", "uuid": "f3f70b01-3a87-4b5a-a77e-6238f6b78123",
            "xl": "/not-executed/xl", "configFile": "/not-executed/domain.cfg",
            "stateDirectory": str(self.directory), "startTimeoutSec": 1,
            "shutdownTimeoutSec": 1, "destroyOnTimeout": False,
        }
        self.receipt = self.directory / "test-branch.json"
        self.xl = FakeXl(self.spec, self.receipt)
        self.patcher = patch.object(domain, "run_xl", self.xl)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.clock = 0
        self.clock_patch = patch.object(domain.time, "monotonic", side_effect=self.tick)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.sleep_patch = patch.object(domain.time, "sleep")
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def tick(self):
        self.clock += 0.25
        return self.clock

    def actions(self):
        return [call for call in self.xl.calls if call[0] in ("create", "shutdown", "destroy") and "--dryrun" not in call]

    def test_create_graceful_stop_and_restart(self):
        domain.start(self.spec, self.receipt)
        saved = json.loads(self.receipt.read_text())
        self.assertEqual(saved["domid"], 42)
        self.assertEqual(saved["phase"], "owned")
        self.assertEqual(self.receipt.stat().st_mode & 0o777, 0o600)
        domain.stop(self.spec, self.receipt)
        self.assertFalse(self.receipt.exists())
        self.assertFalse(self.xl.live)
        domain.start(self.spec, self.receipt)
        self.assertEqual(len([call for call in self.actions() if call[0] == "create"]), 2)

    def test_matching_existing_domain_is_not_adopted(self):
        self.xl.live = [{"domid": 19, "name": self.spec["name"], "uuid": self.spec["uuid"]}]
        with self.assertRaisesRegex(domain.Refused, "adopt"):
            domain.start(self.spec, self.receipt)
        self.assertEqual(self.actions(), [])
        self.assertFalse(self.receipt.exists())

    def test_config_identity_mismatch_fails_before_create(self):
        self.xl.config["c_info"]["name"] = "somebody-else"
        with self.assertRaisesRegex(domain.Refused, "name/UUID"):
            domain.start(self.spec, self.receipt)
        self.assertEqual(self.actions(), [])
        self.assertFalse(self.receipt.exists())

    def test_automatic_restart_config_is_rejected(self):
        self.xl.config["on_reboot"] = "restart"
        with self.assertRaisesRegex(domain.Refused, "on_reboot"):
            domain.start(self.spec, self.receipt)
        self.assertEqual(self.actions(), [])

    def test_ambiguous_create_retains_intent_and_refuses_retry(self):
        self.xl.create_error = True
        with self.assertRaises(domain.Refused):
            domain.start(self.spec, self.receipt)
        self.assertEqual(json.loads(self.receipt.read_text())["phase"], "creating")
        with self.assertRaisesRegex(domain.Refused, "recovery"):
            domain.start(self.spec, self.receipt)
        with self.assertRaisesRegex(domain.Refused, "Ambiguous"):
            domain.stop(self.spec, self.receipt)
        self.assertEqual(len(self.actions()), 1)
        self.assertTrue(self.receipt.exists())

    def test_stop_without_ownership_never_touches_matching_domain(self):
        self.xl.live = [{"domid": 42, "name": self.spec["name"], "uuid": self.spec["uuid"]}]
        with self.assertRaisesRegex(domain.Refused, "No ownership"):
            domain.stop(self.spec, self.receipt)
        self.assertEqual(self.actions(), [])

    def test_changed_domain_id_refuses_shutdown(self):
        domain.start(self.spec, self.receipt)
        self.xl.live[0]["domid"] = 43
        with self.assertRaisesRegex(domain.Refused, "ID changed"):
            domain.stop(self.spec, self.receipt)
        self.assertFalse(any(call[0] == "shutdown" for call in self.actions()))

    def test_changed_uuid_refuses_shutdown(self):
        domain.start(self.spec, self.receipt)
        self.xl.live[0]["uuid"] = "a3f70b01-3a87-4b5a-a77e-6238f6b78123"
        with self.assertRaisesRegex(domain.Refused, "conflicts"):
            domain.stop(self.spec, self.receipt)
        self.assertFalse(any(call[0] == "shutdown" for call in self.actions()))

    def test_graceful_timeout_preserves_guest_and_receipt(self):
        domain.start(self.spec, self.receipt)
        self.xl.graceful = False
        with self.assertRaisesRegex(domain.Refused, "left running"):
            domain.stop(self.spec, self.receipt)
        self.assertTrue(self.xl.live)
        self.assertTrue(self.receipt.exists())
        self.assertFalse(any(call[0] == "destroy" for call in self.actions()))

    def test_explicit_timeout_policy_destroys_only_owned_id(self):
        domain.start(self.spec, self.receipt)
        self.xl.graceful = False
        self.spec["destroyOnTimeout"] = True
        domain.stop(self.spec, self.receipt)
        self.assertIn(("destroy", "42"), self.actions())
        self.assertFalse(self.receipt.exists())

    def test_identity_change_after_shutdown_refuses_force_policy(self):
        domain.start(self.spec, self.receipt)
        self.xl.graceful = False
        self.spec["destroyOnTimeout"] = True
        def replace_domain(spec, *arguments, timeout=15):
            result = self.xl(spec, *arguments, timeout=timeout)
            if arguments[0] == "shutdown":
                self.xl.live[0]["uuid"] = "a3f70b01-3a87-4b5a-a77e-6238f6b78123"
            return result
        with patch.object(domain, "run_xl", side_effect=replace_domain):
            with self.assertRaisesRegex(domain.Refused, "conflicts"):
                domain.stop(self.spec, self.receipt)
        self.assertFalse(any(call[0] == "destroy" for call in self.actions()))

    def test_failed_post_create_receipt_update_preserves_recoverable_intent(self):
        original = domain.write_receipt
        writes = 0
        def write_once(path, value):
            nonlocal writes
            writes += 1
            if writes > 1:
                raise OSError("synthetic receipt write failure")
            original(path, value)
        with patch.object(domain, "write_receipt", side_effect=write_once):
            with self.assertRaises(OSError):
                domain.start(self.spec, self.receipt)
        self.assertEqual(json.loads(self.receipt.read_text())["phase"], "creating")
        self.assertTrue(self.xl.live)
        domain.stop(self.spec, self.receipt)
        self.assertFalse(self.xl.live)
        self.assertFalse(self.receipt.exists())

    def test_stop_is_idempotent_after_guest_has_exited(self):
        domain.start(self.spec, self.receipt)
        self.xl.live = []
        domain.stop(self.spec, self.receipt)
        domain.stop(self.spec, self.receipt)
        self.assertFalse(self.receipt.exists())

    def test_status_before_start_does_not_create_state(self):
        spec = self.spec | {"stateDirectory": str(self.directory / "absent-state")}
        spec_path = self.directory / "spec.json"
        spec_path.write_text(json.dumps(spec))
        output = io.StringIO()
        with patch.object(domain.sys, "argv", ["xen-domain", "--spec", str(spec_path), "status"]):
            with patch.object(domain.signal, "signal"), contextlib.redirect_stdout(output):
                self.assertEqual(domain.main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "absent")
        self.assertFalse(result["owned"])
        self.assertIsNone(result["receipt_phase"])
        self.assertFalse(Path(spec["stateDirectory"]).exists())
        self.assertEqual(self.actions(), [])

    def test_status_observes_owned_domain_without_writing(self):
        domain.start(self.spec, self.receipt)
        self.xl.calls.clear()
        before = self.receipt.read_bytes(), self.receipt.stat().st_mtime_ns
        result = domain.status(self.spec, self.receipt)
        self.assertEqual(result["status"], "present")
        self.assertEqual(result["domid"], 42)
        self.assertEqual(result["receipt_phase"], "owned")
        self.assertTrue(result["owned"])
        self.assertEqual((self.receipt.read_bytes(), self.receipt.stat().st_mtime_ns), before)
        self.assertFalse((self.directory / "manager.lock").exists())
        self.assertEqual(self.actions(), [])

    def test_status_does_not_adopt_or_retire_ambiguous_ownership(self):
        self.xl.live = [{"domid": 42, "name": self.spec["name"], "uuid": self.spec["uuid"]}]
        result = domain.status(self.spec, self.receipt)
        self.assertEqual(result["status"], "present")
        self.assertFalse(result["owned"])
        self.assertFalse(self.receipt.exists())
        intent = {"schema": 1, "name": self.spec["name"], "uuid": self.spec["uuid"], "phase": "creating"}
        domain.write_receipt(self.receipt, intent)
        before = self.receipt.read_bytes()
        for live in (self.xl.live, []):
            self.xl.live = live
            result = domain.status(self.spec, self.receipt)
            self.assertFalse(result["owned"])
            self.assertEqual(result["receipt_phase"], "creating")
            self.assertEqual(self.receipt.read_bytes(), before)
        self.assertEqual(self.actions(), [])

    def test_status_does_not_retire_exited_domain_receipt(self):
        domain.start(self.spec, self.receipt)
        self.xl.live = []
        self.xl.calls.clear()
        before = self.receipt.read_bytes()
        result = domain.status(self.spec, self.receipt)
        self.assertEqual(result["status"], "absent")
        self.assertEqual(result["receipt_phase"], "owned")
        self.assertFalse(result["owned"])
        self.assertEqual(self.receipt.read_bytes(), before)
        self.assertEqual(self.actions(), [])

    def test_status_refuses_changed_identity_without_actions(self):
        domain.start(self.spec, self.receipt)
        self.xl.calls.clear()
        self.xl.live[0]["domid"] = 43
        with self.assertRaisesRegex(domain.Refused, "ID changed"):
            domain.status(self.spec, self.receipt)
        self.xl.live[0]["domid"] = 42
        self.xl.live[0]["uuid"] = "a3f70b01-3a87-4b5a-a77e-6238f6b78123"
        with self.assertRaisesRegex(domain.Refused, "conflicts"):
            domain.status(self.spec, self.receipt)
        self.assertEqual(self.actions(), [])

    def test_status_refuses_changed_declaration_without_adoption(self):
        domain.start(self.spec, self.receipt)
        self.xl.calls.clear()
        changed = self.spec | {"uuid": "a3f70b01-3a87-4b5a-a77e-6238f6b78123"}
        before = self.receipt.read_bytes()
        with self.assertRaisesRegex(domain.Refused, "different domain configuration"):
            domain.status(changed, self.receipt)
        self.assertEqual(self.receipt.read_bytes(), before)
        self.assertEqual(self.xl.calls, [])

    def test_incomplete_json_inventory_fails_closed(self):
        replies = ["Name ID Mem VCPUs State Time(s)\nDomain-0 0 2048 2 r----- 0.0\nother 9 1024 1 r----- 0.0\n", "[]"]
        with patch.object(domain, "run_xl", side_effect=replies):
            with self.assertRaisesRegex(domain.Refused, "omitted"):
                domain.read_inventory(self.spec)

    def test_manager_lock_refuses_concurrent_lifecycle(self):
        with domain.manager_lock(self.directory):
            with self.assertRaises(BlockingIOError):
                with domain.manager_lock(self.directory):
                    self.fail("Concurrent lock was admitted")


if __name__ == "__main__":
    unittest.main()
