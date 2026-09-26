import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "supervisor", Path(__file__).parents[1] / "scripts/lab-supervisor.py")
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class FilesystemTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def request(self):
        scratch = self.root / "ryxos-lab-fixture"
        scratch.mkdir(mode=0o700)
        output = self.root / "evidence"
        output.mkdir(mode=0o700)
        request = {"nonce": "fixture", "unit": "ryxos-lab-fixture.service",
                   "description": "RyxOS disposable lab fixture", "run_dir": str(output),
                   "scratch": str(scratch), "scratch_identity": lab.private_directory(scratch),
                   "memory_max_bytes": 6 * lab.GIB, "cpus": [2, 6], "path": "/usr/bin",
                   "launch_state": "not_submitted",
                   "limits": {"vcpus": 2, "disk_reserve_gib": 21, "memory_reserve_gib": 4,
                              "timeout_seconds": 60, "log_limit_mib": 1}}
        lab.write_json(scratch / "ownership.json", {"nonce": "fixture"})
        return request


class AdmissionTests(FilesystemTest):
    def test_multicall_tool_keeps_its_invocation_name(self):
        implementation = self.root / "multicall"
        implementation.write_text(
            f"#!{sys.executable}\nimport pathlib, sys\n"
            "print(pathlib.Path(sys.argv[0]).name)\n")
        implementation.chmod(0o700)
        command = self.root / "nix-store"
        command.symlink_to(implementation)
        result = subprocess.run([lab.executable(str(command))],
                                check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "nix-store")

    def driver(self, extra="", accelerator="kvm", memory=4096):
        store = self.root / "store"
        store.mkdir(exist_ok=True)
        package = store / ("a" * 32 + "-fixture-" + str(len(list(store.iterdir()))))
        package.mkdir(exist_ok=True)
        driver, config, launcher = (package / name for name in ("driver", "config.json", "launcher"))
        driver.write_text(f"#!/bin/sh\nexec /nix/store/fake/bin/driver --config {config}\n")
        config.write_text(json.dumps({"vms": {"guest": {"start_script": str(launcher)}},
                                      "containers": {}, "vlans": [], "enable_ssh_backdoor": False}))
        launcher.write_text(f"exec /nix/store/fake/bin/qemu-system-x86_64 -machine accel={accelerator} \\\n"
                            f"  -m {memory} \\\n  -smp 2 \\\n  -no-user-config \\\n  -nic none \\\n{extra}\n")
        for path in (driver, launcher):
            path.chmod(0o555)
        config.chmod(0o444)
        return store, driver, config, launcher

    def test_reads_actual_pinned_launcher_budgets(self):
        store, driver, _, _ = self.driver()
        result = lab.inspect_driver(driver, 4096, 2, store)
        self.assertEqual(result["machines"][0]["memory_mib"], 4096)
        with self.assertRaisesRegex(lab.Refusal, "guest memory"):
            lab.inspect_driver(driver, 2048, 2, store)

    def test_software_fallback_and_later_acceleration_override_refused(self):
        store, driver, _, _ = self.driver(accelerator="kvm:tcg")
        with self.assertRaises(lab.Refusal):
            lab.inspect_driver(driver, 4096, 2, store)
        store, driver, _, _ = self.driver(extra="  -accel tcg")
        with self.assertRaises(lab.Refusal):
            lab.inspect_driver(driver, 4096, 2, store)

    def test_mutable_driver_and_implicit_network_driver_refused(self):
        store, driver, config, _ = self.driver()
        config.chmod(0o644)
        value = json.loads(config.read_text())
        value["vlans"] = [1]
        config.write_text(json.dumps(value))
        config.chmod(0o444)
        with self.assertRaisesRegex(lab.Refusal, "closed QEMU"):
            lab.inspect_driver(driver, 4096, 2, store)
        driver.chmod(0o755)
        with self.assertRaisesRegex(lab.Refusal, "immutable"):
            lab.inspect_driver(driver, 4096, 2, store)

    def test_explicit_network_and_share_arguments_are_not_hidden_by_empty_vlans(self):
        for arguments in ("-netdev user,id=escape", "-nic user", "-nic=user",
                          "-virtfs local,path=/home,mount_tag=escape", "-fsdev=local,id=escape,path=/home",
                          "-device vhost-user-fs-pci,chardev=escape", "-device virtio-net-pci,netdev=escape",
                          "-nic", "-device"):
            with self.subTest(arguments=arguments):
                store, driver, _, _ = self.driver(extra="  " + arguments)
                with self.assertRaises(lab.Refusal):
                    lab.inspect_driver(driver, 4096, 2, store)

    def test_complete_smt_allocation_and_overhead_admission(self):
        rows = [{"cpu": cpu, "core": cpu % 4, "socket": 0, "online": True} for cpu in range(8)]
        limits = {"memory_mib": 4096, "qemu_overhead_mib": 2048, "vcpus": 2,
                  "reserve_cores": 2, "disk_reserve_gib": 21, "memory_reserve_gib": 4}
        sample = {"memory_available_bytes": 10 * lab.GIB, "disk_free_bytes": {"scratch": 30 * lab.GIB}}
        self.assertEqual(lab.admit(sample, limits, rows, set(range(8)))["lab_cpus"], [2, 6])
        with self.assertRaises(ValueError):
            lab.admit(sample, limits, rows, {0, 1, 2, 3, 4, 5})
        sample["memory_available_bytes"] -= 1
        with self.assertRaisesRegex(lab.Refusal, "RAM"):
            lab.admit(sample, limits, rows, set(range(8)))


class EffectiveLimitsTests(FilesystemTest):
    def prepare(self):
        request = self.request()
        proc = self.root / "proc-cgroup"
        proc.write_text("0::/user.slice/" + request["unit"] + "\n")
        root = self.root / "cgroup"
        group = root / "user.slice" / request["unit"]
        group.mkdir(parents=True)
        for name, value in {"cpu.max": "200000 100000", "memory.max": str(6 * lab.GIB),
                            "memory.swap.max": "0"}.items():
            (group / name).write_text(value)
        return request, root, group, proc

    def test_missing_optional_io_is_not_reported_as_enforced(self):
        request, root, _, proc = self.prepare()
        result = lab.effective_limits(request, root, proc, lambda _: {2, 6})
        self.assertTrue(result["memory_verified"])
        self.assertFalse(result["io_weight_applied"])
        self.assertIsNone(result["io_bandwidth_cap"])
        self.assertFalse(result["cpuset_matches_allocation"])

    def test_accepted_properties_without_effective_controllers_block_launch(self):
        request, root, group, proc = self.prepare()
        (group / "memory.max").unlink()
        with self.assertRaisesRegex(lab.Refusal, "delegated"):
            lab.effective_limits(request, root, proc, lambda _: {2, 6})

    def test_unbounded_swap_wrong_affinity_and_wrong_service_are_refused(self):
        request, root, group, proc = self.prepare()
        (group / "memory.swap.max").write_text("max")
        with self.assertRaises(lab.Refusal):
            lab.effective_limits(request, root, proc, lambda _: {2, 6})
        (group / "memory.swap.max").write_text("0")
        with self.assertRaisesRegex(lab.Refusal, "affinity"):
            lab.effective_limits(request, root, proc, lambda _: set(range(8)))
        proc.write_text("0::/user.slice/unrelated.service\n")
        with self.assertRaisesRegex(lab.Refusal, "exact owned"):
            lab.effective_limits(request, root, proc, lambda _: {2, 6})


class EnvironmentTests(FilesystemTest):
    def test_real_child_receives_no_qemu_overrides_credentials_or_user_config(self):
        request = self.request()
        scratch, output = Path(request["scratch"]), Path(request["run_dir"])
        for name in ("home", "tmp", "runtime", "config", "cache", "state"):
            (scratch / name).mkdir(mode=0o700)
        driver = self.root / "benign-driver"
        driver.write_text(f"#!{sys.executable}\nimport json,os,pathlib\n"
                          "pathlib.Path(os.environ['out'],'environment.json').write_text(json.dumps(dict(os.environ)))\n")
        driver.chmod(0o700)
        request["driver"] = str(driver)
        lab.write_json(output / "request.json", request)
        inherited = {"QEMU_OPTS": "-accel tcg", "QEMU_NET_OPTS": "hostfwd=tcp::22-:22",
                     "QEMU_KERNEL_PARAMS": "init=/bin/sh", "NIXPKGS_QEMU_KERNEL_guest": "/foreign",
                     "NIX_DISK_IMAGE": "/important", "NIX_EFI_VARS": "/important", "NIX_SWTPM_DIR": "/important",
                     "SHARED_DIR": "/home", "USE_TMPDIR": "1", "SSH_AUTH_SOCK": "/secret",
                     "API_KEY": "synthetic-secret", "PYTHONPATH": "/foreign", "HOME": "/foreign"}
        with patch.dict(os.environ, inherited):
            self.assertEqual(lab.inside(output / "request.json", verify=lambda _: {"verified": True}), 0)
        child = json.loads((output / "environment.json").read_text())
        self.assertEqual(child["HOME"], str(scratch / "home"))
        for name in inherited:
            if name != "HOME":
                self.assertNotIn(name, child)
        self.assertEqual(lab.read_json(output / "inside.json")["status"], "passed")

    def test_driver_is_not_started_when_effective_limits_fail(self):
        request = self.request()
        output = Path(request["run_dir"])
        lab.write_json(output / "request.json", request)
        run = Mock()
        def refuse(_):
            raise lab.Refusal("memory controller absent")
        self.assertEqual(lab.inside(output / "request.json", verify=refuse, run=run), 1)
        run.assert_not_called()
        self.assertFalse(lab.read_json(output / "inside.json")["driver_started"])


class OwnershipTests(FilesystemTest):
    def test_driver_gc_root_is_registered_without_builds_and_survives_cleanup(self):
        request = self.request()
        output = Path(request["run_dir"])
        retained = self.root / "immutable-driver-output"
        retained.mkdir()
        admitted = {"store_path": str(retained)}
        calls = []
        def register(command, **kwargs):
            calls.append(command)
            Path(command[command.index("--add-root") + 1]).symlink_to(retained)
            return subprocess.CompletedProcess(command, 0, "", "")
        record = lab.retain_driver(output, admitted, "/fixed/nix-store", {}, register)
        self.assertEqual(calls[0][-9:], ["--option", "substitute", "false", "--option", "max-jobs", "0", "--option", "builders", ""])
        lab.remove_scratch(request)
        self.assertEqual(Path(record["path"]).resolve(), retained)
        self.assertTrue(record["retained_after_run"])

    def test_failed_gc_registration_cannot_qualify_an_unrooted_driver(self):
        output = self.root / "evidence"
        output.mkdir(mode=0o700)
        run = Mock(return_value=subprocess.CompletedProcess([], 1, "", "unavailable"))
        with self.assertRaisesRegex(lab.Refusal, "retain"):
            lab.retain_driver(output, {"store_path": "/unregistered"}, "/fixed/nix-store", {}, run)

    def test_single_lab_lock_and_persistent_lease_survive_process_lock_release(self):
        with lab.Slot(self.root) as first:
            with self.assertRaisesRegex(lab.Refusal, "another lab"):
                with lab.Slot(self.root):
                    self.fail("second lock acquired")
            lab.write_json(first.active, {"nonce": "stale"}, exclusive=True)
        with lab.Slot(self.root) as recovered:
            self.assertEqual(lab.read_json(recovered.active), {"nonce": "stale"})

    def test_symlink_or_hardlinked_control_file_is_not_read(self):
        target = self.root / "target"
        lab.write_json(target, {"preserve": True})
        link = self.root / "link"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            lab.read_json(link)
        link.unlink()
        os.link(target, link)
        with self.assertRaises(lab.Refusal):
            lab.read_json(link)

    def test_replaced_scratch_and_neighbor_are_preserved(self):
        request = self.request()
        scratch = Path(request["scratch"])
        original = self.root / "original"
        scratch.rename(original)
        scratch.mkdir(mode=0o700)
        (scratch / "neighbor").write_text("preserve")
        with self.assertRaisesRegex(lab.Refusal, "identity changed"):
            lab.remove_scratch(request)
        self.assertEqual((scratch / "neighbor").read_text(), "preserve")
        self.assertTrue(original.exists())

    def test_owned_scratch_symlinks_do_not_remove_their_targets(self):
        request = self.request()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "preserve").write_text("yes")
        (Path(request["scratch"]) / "link").symlink_to(outside)
        self.assertTrue(lab.remove_scratch(request)["removed"])
        self.assertEqual((outside / "preserve").read_text(), "yes")

    def test_foreign_service_is_never_stopped(self):
        request = self.request()
        run = Mock(return_value=subprocess.CompletedProcess([], 0,
                   "LoadState=loaded\nActiveState=active\nDescription=unrelated\nTransient=yes\nControlGroup=\n", ""))
        controller = lab.Controller({"systemctl": "/fixed/systemctl", "path": "/fixed"}, self.root, run)
        with self.assertRaisesRegex(lab.Refusal, "ownership changed"):
            controller.stop_owned(request)
        self.assertEqual(run.call_count, 1)

    def test_inactive_unit_with_remaining_cgroup_processes_is_not_clean(self):
        request = self.request()
        group = self.root / "cgroup" / request["unit"]
        group.mkdir(parents=True)
        (group / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        def state(active):
            return subprocess.CompletedProcess([], 0,
                "LoadState=loaded\nActiveState=" + active + "\nDescription=" + request["description"]
                + "\nTransient=yes\nControlGroup=/" + request["unit"] + "\n", "")
        run = Mock(side_effect=[state("active"), subprocess.CompletedProcess([], 0, "", ""), state("inactive")])
        controller = lab.Controller({"systemctl": "/fixed/systemctl", "path": "/fixed"},
                                    self.root, run, self.root / "cgroup")
        with self.assertRaisesRegex(lab.Refusal, "still contains processes"):
            controller.stop_owned(request)
        self.assertEqual(run.call_args_list[1].args[0],
                         ["/fixed/systemctl", "--user", "stop", request["unit"]])

    def test_cleanup_failure_retains_active_lease_and_scratch(self):
        request = self.request()
        with lab.Slot(self.root) as slot:
            lab.write_json(slot.active, {"nonce": request["nonce"], "run_dir": request["run_dir"]}, exclusive=True)
            controller = Mock()
            controller.stop_owned.side_effect = lab.Refusal("unit remains active")
            with self.assertRaises(lab.Refusal):
                lab.cleanup(request, slot, controller)
            self.assertTrue(slot.active.exists())
            self.assertTrue(Path(request["scratch"]).exists())

    def test_absent_unit_during_submission_never_discards_recovery_state(self):
        request = self.request()
        request["launch_state"] = "submitting"
        run = Mock(return_value=subprocess.CompletedProcess([], 0, "LoadState=not-found\n", ""))
        controller = lab.Controller({"systemctl": "/fixed/systemctl", "path": "/fixed"}, self.root, run)
        with lab.Slot(self.root) as slot:
            lab.write_json(slot.active, {"nonce": request["nonce"], "run_dir": request["run_dir"]}, exclusive=True)
            with self.assertRaisesRegex(lab.Refusal, "submission is unsettled"):
                lab.cleanup(request, slot, controller)
            self.assertTrue(slot.active.exists())
            self.assertTrue(Path(request["scratch"]).exists())
            self.assertEqual(run.call_count, 1)
            # A later known launcher exit makes absence conclusive.
            request["launch_state"] = "settled"
            self.assertTrue(lab.cleanup(request, slot, controller)["lease_removed"])

    def test_submission_intent_is_durable_even_if_popen_is_interrupted(self):
        request = self.request()
        path = Path(request["run_dir"]) / "request.json"
        lab.write_json(path, request)
        def interrupted(*_args, **_kwargs):
            self.assertEqual(lab.read_json(path)["launch_state"], "submitting")
            raise lab.Refusal("synthetic interruption during Popen")
        with patch.object(lab, "service_command", return_value=["unexecuted-launcher"]):
            with patch.object(lab.subprocess, "Popen", side_effect=interrupted):
                with self.assertRaisesRegex(lab.Refusal, "interruption"):
                    lab.submit(request, {}, {}, None)
        self.assertEqual(lab.read_json(path)["launch_state"], "submitting")

    def test_launcher_reap_timeout_preserves_lease_and_scratch(self):
        request = self.request()
        controller = Mock()
        controller.stop_owned.return_value = {"gone": True}
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("synthetic-launcher", 10)
        with lab.Slot(self.root) as slot:
            lab.write_json(slot.active, {"nonce": request["nonce"], "run_dir": request["run_dir"]}, exclusive=True)
            with self.assertRaises(subprocess.TimeoutExpired):
                lab.cleanup(request, slot, controller, process)
            controller.stop_owned.assert_called_once_with(request)
            self.assertTrue(slot.active.exists())
            self.assertTrue(Path(request["scratch"]).exists())

    def test_inside_entry_proves_submission_even_if_outer_parent_was_interrupted(self):
        request = self.request()
        request["launch_state"] = "submitting"
        output = Path(request["run_dir"])
        lab.write_json(output / "inside.json", {
            "schema_version": 1, "status": "blocked", "driver_started": False,
        })
        run = Mock(return_value=subprocess.CompletedProcess([], 0, "LoadState=not-found\n", ""))
        controller = lab.Controller({"systemctl": "/fixed/systemctl", "path": "/fixed"}, self.root, run)
        with lab.Slot(self.root) as slot:
            lab.write_json(slot.active, {"nonce": request["nonce"], "run_dir": request["run_dir"]}, exclusive=True)
            self.assertTrue(lab.cleanup(request, slot, controller)["lease_removed"])

    def test_legacy_or_malformed_submission_evidence_cannot_prove_absence(self):
        request = self.request()
        del request["launch_state"]
        output = Path(request["run_dir"])
        lab.write_json(output / "inside.json", {"status": "passed"})
        run = Mock(return_value=subprocess.CompletedProcess([], 0, "LoadState=not-found\n", ""))
        controller = lab.Controller({"systemctl": "/fixed/systemctl", "path": "/fixed"}, self.root, run)
        with self.assertRaisesRegex(lab.Refusal, "submission is unsettled"):
            controller.stop_owned(request)

    def test_successful_owned_cleanup_retains_durable_evidence(self):
        request = self.request()
        output = Path(request["run_dir"])
        lab.write_json(output / "receipt.json", {"status": "failed", "driver_started": True})
        with lab.Slot(self.root) as slot:
            lab.write_json(slot.active, {"nonce": request["nonce"], "run_dir": request["run_dir"]}, exclusive=True)
            controller = Mock()
            controller.stop_owned.return_value = {"gone": True}
            result = lab.cleanup(request, slot, controller)
            self.assertTrue(result["lease_removed"])
        self.assertTrue(lab.read_json(output / "receipt.json")["driver_started"])
        self.assertFalse(Path(request["scratch"]).exists())


class MonitorTests(FilesystemTest):
    def test_reserve_crossing_records_minimum_and_requests_stop_by_failure(self):
        request = self.request()
        process = Mock()
        process.poll.return_value = None
        receipt = {}
        sample = lambda: {"disk_free_bytes": {"tmp": 20 * lab.GIB}, "memory_available_bytes": 8 * lab.GIB}
        with self.assertRaisesRegex(lab.Refusal, "reserve crossed"):
            lab.monitor(process, request, receipt, sample, clock=lambda: 0, sleep=lambda _: None)
        self.assertEqual(receipt["minimum_disk_free_bytes"], 20 * lab.GIB)

    def test_oversized_log_stops_monitoring_instead_of_filling_evidence_disk(self):
        request = self.request()
        (Path(request["run_dir"]) / "driver.log").write_bytes(b"x" * (lab.MIB + 1))
        sample = lambda: {"disk_free_bytes": {"tmp": 30 * lab.GIB}, "memory_available_bytes": 8 * lab.GIB}
        process = Mock()
        process.poll.return_value = None
        with self.assertRaisesRegex(lab.Refusal, "log exceeded"):
            lab.monitor(process, request, {}, sample, clock=lambda: 0, sleep=lambda _: None)


class ReceiptTests(FilesystemTest):
    def test_missing_inside_receipt_does_not_fabricate_nonexecution_or_success(self):
        receipt = {"status": "passed", "driver_started": None}
        lab.finish_receipt(self.root, receipt, True)
        self.assertEqual(receipt["status"], "failed")
        self.assertIsNone(receipt["driver_started"])
        self.assertIn("unknown", receipt["inside_receipt_error"])

    def test_disk_full_preserves_actual_execution_and_cleanup_for_stdout(self):
        lab.write_json(self.root / "inside.json", {"status": "passed", "driver_started": True})
        receipt = {"status": "passed", "driver_started": None, "cleanup": {"unit": {"gone": True}}}
        with patch.object(lab, "write_json", side_effect=OSError("disk full")):
            lab.finish_receipt(self.root, receipt, True)
        self.assertEqual(receipt["status"], "failed")
        self.assertTrue(receipt["driver_started"])
        self.assertTrue(receipt["cleanup"]["unit"]["gone"])
        self.assertEqual(receipt["receipt_write_error"], "disk full")


if __name__ == "__main__":
    unittest.main()
