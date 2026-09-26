import unittest
from unittest.mock import Mock, patch

import probe


class CapabilityProbeTests(unittest.TestCase):
    def run_probe(self, cpu="flags : svm npt\n", nested="Y\n", ioctl=(12, 11)):
        with (
            patch.object(probe.Path, "read_text", side_effect=[cpu, nested]),
            patch.object(probe.subprocess, "run") as command,
            patch.object(probe.os, "open", return_value=10) as opened,
            patch.object(probe.os, "close") as closed,
            patch.object(probe.fcntl, "ioctl", side_effect=ioctl),
        ):
            result = probe.probe()
        return result, command, opened, closed

    def test_missing_cpu_virtualization_refuses_module_and_device(self):
        result, command, opened, _ = self.run_probe(cpu="flags : fpu\n")
        self.assertEqual(result["status"], "failed")
        command.assert_not_called()
        opened.assert_not_called()

    def test_empty_vm_success_closes_both_descriptors(self):
        result, _, _, closed = self.run_probe()
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["vm_booted"])
        self.assertEqual([call.args[0] for call in closed.call_args_list], [11, 10])

    def test_disabled_nested_parameter_refuses_device(self):
        result, _, opened, _ = self.run_probe(nested="N\n")
        self.assertEqual(result["status"], "failed")
        opened.assert_not_called()

    def test_wrong_api_closes_device_without_vm(self):
        result, _, _, closed = self.run_probe(ioctl=[11])
        self.assertEqual(result["status"], "failed")
        closed.assert_called_once_with(10)

    def test_failed_vm_creation_closes_device(self):
        result, _, _, closed = self.run_probe(ioctl=[12, OSError("not supported")])
        self.assertEqual(result["status"], "failed")
        closed.assert_called_once_with(10)


class BootProbeTests(unittest.TestCase):
    def test_explicit_modes_and_unknown_modes(self):
        self.assertEqual(probe.probe_mode("console=hvc0 ryxos.probe=boot"), "boot")
        self.assertEqual(probe.probe_mode("ryxos.probe=nested"), "nested")
        self.assertEqual(probe.probe_mode("console=hvc0"), "nested")
        for cmdline in ("ryxos.probe=unknown", "ryxos.probe=boot ryxos.probe=nested"):
            with self.subTest(cmdline=cmdline), self.assertRaises(RuntimeError):
                probe.probe_mode(cmdline)

    def run_boot_probe(self, *, boot_id="12345678-1234-4234-8234-123456789abc", command=None):
        if command is None:
            command = Mock(stdout="RYXOS_XEN_BOOT_OK\n", returncode=0)
        with (
            patch.object(probe.Path, "is_dir", return_value=True),
            patch.object(probe.Path, "read_text", return_value=boot_id),
            patch.object(probe.subprocess, "run", return_value=command) as run,
            patch.object(probe.os, "open") as opened,
            patch.object(probe.fcntl, "ioctl") as ioctl,
        ):
            result = probe.probe("boot")
        opened.assert_not_called()
        ioctl.assert_not_called()
        return result, run

    def test_boot_executes_fixed_command_without_kvm_probe(self):
        result, run = self.run_boot_probe()
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["guest_booted"])
        self.assertEqual(result["nested_probe"], "not_requested")
        self.assertNotIn("kvm_api", result)
        run.assert_called_once_with(
            [probe.sys.executable, "-c", "print('RYXOS_XEN_BOOT_OK')"],
            check=True, capture_output=True, text=True, timeout=5,
        )

    def test_bad_boot_identity_refuses_before_command(self):
        result, run = self.run_boot_probe(boot_id="not-a-boot-id")
        self.assertEqual(result["status"], "failed")
        run.assert_not_called()

    def test_wrong_command_response_cannot_pass(self):
        result, _ = self.run_boot_probe(command=Mock(stdout="unexpected\n", returncode=0))
        self.assertEqual(result["status"], "failed")

    def test_command_failure_is_preserved(self):
        with (
            patch.object(probe.Path, "is_dir", return_value=True),
            patch.object(probe.Path, "read_text", return_value="12345678-1234-4234-8234-123456789abc"),
            patch.object(probe.subprocess, "run", side_effect=probe.subprocess.CalledProcessError(1, "probe")),
        ):
            result = probe.probe("boot")
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("guest_booted", result)


if __name__ == "__main__":
    unittest.main()
