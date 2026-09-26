import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "preflight", Path(__file__).parents[1] / "scripts/lab-preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class PreflightTests(unittest.TestCase):
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
