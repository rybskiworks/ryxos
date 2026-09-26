import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
