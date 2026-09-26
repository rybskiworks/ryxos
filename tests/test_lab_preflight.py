import importlib.util
import contextlib
import io
import json
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest import mock
import struct

spec = importlib.util.spec_from_file_location(
    "preflight", Path(__file__).parents[1] / "scripts/lab-preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class PreflightTests(unittest.TestCase):
    def test_main_keeps_xen_nesting_advisory_unless_explicitly_required(self):
        files = {
            "/proc/meminfo": "MemAvailable: 67108864 kB\n",
            "/proc/cpuinfo": "vendor_id: AuthenticAMD\n",
            "/sys/module/kvm_amd/parameters/nested": "1\n",
            "/sys/fs/cgroup/cgroup.controllers": "cpu memory\n",
        }
        for strict in [False, True]:
            for failure in [False, True]:
                with self.subTest(strict=strict, ioctl_failure=failure):
                    output = io.StringIO()
                    argv = ["preflight"] + (["--require-xen-nesting"] if strict else [])
                    probe = (OSError("KVM CPUID unavailable") if failure else
                             {"available": False, "missing": ["decodeassists"]})
                    with contextlib.ExitStack() as stack:
                        stack.enter_context(mock.patch("sys.argv", argv))
                        stack.enter_context(mock.patch.object(preflight.platform, "machine", return_value="x86_64"))
                        stack.enter_context(mock.patch.object(Path, "read_text", autospec=True,
                                                             side_effect=lambda path: files[str(path)]))
                        stack.enter_context(mock.patch.object(Path, "stat", return_value=SimpleNamespace(st_mode=stat.S_IFCHR)))
                        stack.enter_context(mock.patch.object(preflight.shutil, "disk_usage", return_value=SimpleNamespace(free=64 * preflight.GIB)))
                        stack.enter_context(mock.patch.object(preflight.shutil, "which", return_value="/nix/bin/nix"))
                        stack.enter_context(mock.patch.object(preflight.os, "sched_getaffinity", return_value=set(range(8))))
                        stack.enter_context(mock.patch.object(preflight.os, "open", return_value=20))
                        stack.enter_context(mock.patch.object(preflight.os, "close"))
                        stack.enter_context(mock.patch.object(preflight.fcntl, "ioctl", return_value=12))
                        stack.enter_context(mock.patch.object(preflight.subprocess, "run", side_effect=[
                            SimpleNamespace(stdout=json.dumps({"cpus": self.topology()})),
                            SimpleNamespace(stdout="nix test-version\n")]))
                        stack.enter_context(mock.patch.object(preflight, "xen_amd_nested_prerequisites",
                                                             **({"side_effect": probe} if failure else {"return_value": probe})))
                        stack.enter_context(contextlib.redirect_stdout(output))
                        code = preflight.main()
                    result = json.loads(output.getvalue())
                    self.assertEqual(code, 1 if strict else 0)
                    self.assertEqual(result["status"], "blocked" if strict else "ready")
                    self.assertEqual(bool(result["errors"]), strict)

    def cpuid_result(self, features, count=1):
        def respond(fd, request, data, mutate):
            self.assertEqual(request, 0xC008AE05)
            self.assertTrue(mutate)
            struct.pack_into("=I", data, 0, count)
            struct.pack_into("=10I", data, 8, 0x8000000A, 0, 0, 1, 8, 0,
                             features, 0, 0, 0)
        return mock.patch.object(preflight.fcntl, "ioctl", side_effect=respond)

    def test_svm_presence_does_not_prove_xen_nested_prerequisites(self):
        with self.cpuid_result(0x1001943B):
            result = preflight.xen_amd_nested_prerequisites(7)
        self.assertFalse(result["available"])
        self.assertEqual(result["missing"], ["flushbyasid", "decodeassists"])

    def test_prerequisites_do_not_claim_a_booted_nested_workload(self):
        with self.cpuid_result(0xCB):
            result = preflight.xen_amd_nested_prerequisites(7)
        self.assertTrue(result["available"])
        self.assertFalse(result["nested_workload_qualified"])

    def test_missing_or_oversized_cpuid_response_is_rejected(self):
        for count in [0, 257]:
            with self.subTest(count=count), self.cpuid_result(0xCB, count):
                with self.assertRaises(ValueError):
                    preflight.xen_amd_nested_prerequisites(7)

    def topology(self):
        return [{"cpu": cpu, "core": cpu % 4, "socket": 0, "online": True}
                for cpu in range(8)]

    def test_reserves_complete_cores_and_keeps_smt_siblings_together(self):
        result = preflight.cpu_allocation(self.topology(), set(range(8)), 4, 2)
        self.assertEqual(result["lab_cpus"], [2, 6, 3, 7])
        self.assertEqual(result["reserved_cores"], [[0, 4], [1, 5]])
        self.assertFalse(result["selection_applied"])

    def test_partial_allowed_core_does_not_silently_split_siblings(self):
        with self.assertRaises(ValueError):
            preflight.cpu_allocation(self.topology(), set(range(7)), 4, 2)

    def test_qemu_overhead_and_host_reserve_are_not_guest_memory(self):
        self.assertTrue(preflight.capacity_errors(
            30 * preflight.GIB, 12 * preflight.GIB, 8192, 2048, 21, 4))
        self.assertFalse(preflight.capacity_errors(
            30 * preflight.GIB, 14 * preflight.GIB, 8192, 2048, 21, 4))

    def test_disk_reserve_is_independent_of_ram(self):
        self.assertEqual(preflight.capacity_errors(
            20 * preflight.GIB, 64 * preflight.GIB, 8192, 2048, 21, 4),
            ["disk free space is below the reserve"])
