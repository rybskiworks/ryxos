import errno
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("lifecycle_probe", Path(__file__).with_name("lifecycle-probe.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeTestCase(unittest.TestCase):
    def setUp(self):
        network = Mock()
        network.iterdir.return_value = [Path("/sys/class/net/lo")]
        self.enterContext(patch.object(
            probe, "Path", side_effect=lambda value: network if value == "/sys/class/net" else Path(value)))


class LifecycleProbeTests(ProbeTestCase):
    def test_persisted_marker_counts_boots_and_reset_creates_fresh_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
            first = probe.record_boot(state, first_id)
            self.assertEqual(first["network_interfaces"], ["lo"])
            same_boot = probe.record_boot(state, first_id)
            second = probe.record_boot(state, second_id)
            self.assertEqual((first["boot_count"], same_boot["boot_count"], second["boot_count"]), (1, 1, 2))
            self.assertEqual(first["marker"], second["marker"])
            self.assertEqual(second["command_output"], "RYXOS_XEN_LIFECYCLE_OK")
            self.assertEqual(json.loads((state / "result.json").read_text()), second)
            (state / "result.json").unlink()
            reset = probe.record_boot(state, str(uuid.uuid4()))
            self.assertEqual(reset["boot_count"], 1)
            self.assertNotEqual(reset["marker"], first["marker"])

    def test_corrupt_previous_marker_is_preserved_and_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.json"
            original = '{"schema": 1, "status": "failed"}\n'
            path.write_text(original)
            with self.assertRaisesRegex(RuntimeError, "retained"):
                probe.record_boot(Path(temporary), str(uuid.uuid4()))
            self.assertEqual(path.read_text(), original)


class EgressProbeTests(ProbeTestCase):
    def socket(self, error):
        connection = Mock()
        connection.connect.side_effect = error
        context = Mock()
        context.__enter__ = Mock(return_value=connection)
        context.__exit__ = Mock(return_value=False)
        factory = Mock(return_value=context)
        return factory, connection, context

    def test_real_attempt_contract_requires_kernel_no_route(self):
        factory, connection, context = self.socket(OSError(errno.ENETUNREACH, "synthetic no route"))
        result = probe.probe_egress(factory)
        factory.assert_called_once_with(probe.socket.AF_INET, probe.socket.SOCK_STREAM)
        connection.settimeout.assert_called_once_with(2.0)
        connection.connect.assert_called_once_with(("192.0.2.1", 9))
        context.__exit__.assert_called_once()
        self.assertEqual(result["errno"], errno.ENETUNREACH)
        self.assertTrue(result["attempted"])
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            receipt = probe.record_boot(state, str(uuid.uuid4()), egress=result)
            self.assertEqual(json.loads((state / "result.json").read_text())["egress_probe"], result)
            self.assertEqual(receipt["egress_probe"], result)

    def test_timeout_and_other_errno_do_not_prove_denial(self):
        for error in (TimeoutError("synthetic timeout"), OSError(errno.ECONNREFUSED, "synthetic refusal"),
                      OSError(errno.EACCES, "synthetic permission refusal")):
            with self.subTest(error=type(error).__name__, errno=error.errno):
                factory, connection, context = self.socket(error)
                with self.assertRaisesRegex(RuntimeError, "ENETUNREACH"):
                    probe.probe_egress(factory)
                connection.settimeout.assert_called_once_with(2.0)
                context.__exit__.assert_called_once()

    def test_success_is_refused(self):
        factory, connection, context = self.socket(None)
        with self.assertRaisesRegex(RuntimeError, "connected"):
            probe.probe_egress(factory)
        connection.settimeout.assert_called_once_with(2.0)
        context.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
