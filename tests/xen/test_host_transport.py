import importlib.util
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("host_transport", Path(__file__).with_name("host-transport.py"))
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


class HostTransportTests(unittest.TestCase):
    def fake_proc(self, directory, address="0100007F", owner=True, cgroup="0::/test\n"):
        proc = Path(directory)
        (proc / "net").mkdir()
        (proc / "net/tcp").write_text("header\n 0: " + address + ":56CE 00000000:0000 0A 0:0 0:0 0 0 0 918\n")
        (proc / "net/tcp6").write_text("header\n")
        (proc / "self").mkdir()
        (proc / "self/cgroup").write_text("0::/test\n")
        (proc / "123/fd").mkdir(parents=True)
        (proc / "123/fd/4").symlink_to("socket:[918]" if owner else "socket:[999]")
        (proc / "123/stat").write_text("123 (qemu test) " + " ".join(["S"] + ["0"] * 18 + ["345"]))
        (proc / "123/cgroup").write_text(cgroup)
        return proc

    def client(self, directory):
        client = object.__new__(transport.HostTransport)
        client.ssh = "/nix/store/synthetic-openssh/bin/ssh"
        client.directory = Path(directory)
        client.operator_key = client.directory / "operator"
        client.wrong_key = client.directory / "wrong"
        client.known_hosts = client.directory / "known_hosts"
        client.environment = {"HOME": directory, "LANG": "C", "LC_ALL": "C"}
        return client

    def test_listener_requires_exact_loopback_and_recorded_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            result = transport.listener_owner(123, self.fake_proc(directory))
        self.assertEqual(result["qemu_start_ticks"], "345")
        self.assertTrue(result["same_cgroup"])

    def test_wildcard_ipv6_and_duplicate_listener_refused(self):
        for variant in ("wildcard", "ipv6", "duplicate"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as directory:
                proc = self.fake_proc(directory, address="00000000" if variant == "wildcard" else "0100007F")
                if variant != "wildcard":
                    path = proc / "net" / ("tcp6" if variant == "ipv6" else "tcp")
                    with path.open("a") as output:
                        output.write(" 1: 00000000000000000000000000000000:56CE 0:0 0A 0:0 0:0 0 0 0 919\n")
                with self.assertRaises(AssertionError):
                    transport.listener_owner(123, proc)

    def test_foreign_owner_or_cgroup_refused(self):
        for options in ({"owner": False}, {"cgroup": "0::/foreign\n"}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(AssertionError):
                    transport.listener_owner(123, self.fake_proc(directory, **options))

    def test_busy_port_refuses_without_socket_or_process_operation(self):
        with patch.object(transport, "listener_rows", return_value=[{}]), patch.object(transport.socket, "socket") as socket:
            with self.assertRaisesRegex(AssertionError, "occupied"):
                object.__new__(transport.HostTransport).preflight()
            socket.assert_not_called()

    def test_pinned_ecdsa_host_key_and_private_copy_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic-key"
            source.write_text("public synthetic fixture key")
            public = "ecdsa-sha2-nistp256 AAAAtest fixture"
            with patch.object(transport, "fixture_file", side_effect=lambda value, executable=False: Path(value)):
                client = transport.HostTransport("/synthetic/ssh", source, source, public, directory)
            try:
                self.assertEqual(client.known_hosts.read_text(), "[127.0.0.1]:22222 " + public + "\n")
                self.assertEqual(client.directory.stat().st_mode & 0o777, 0o700)
                for path in (client.operator_key, client.wrong_key, client.known_hosts):
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertNotIn("SSH_AUTH_SOCK", client.environment)
            finally:
                client.temporary.cleanup()

    def test_client_material_requires_private_owned_real_evidence_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "synthetic-key"
            source.write_text("public fixture bytes")
            shared = root / "shared"
            shared.mkdir(mode=0o755)
            shared.chmod(0o755)
            link = root / "link"
            link.symlink_to(root, target_is_directory=True)
            for evidence, wrong_owner in ((shared, False), (link, False), (root, True)):
                with self.subTest(evidence=evidence, wrong_owner=wrong_owner):
                    with patch.object(transport, "fixture_file", side_effect=lambda value, executable=False: source), \
                         patch.object(transport.os, "geteuid", return_value=os.getuid() + int(wrong_owner)), \
                         patch.object(transport.tempfile, "TemporaryDirectory") as temporary:
                        with self.assertRaisesRegex(AssertionError, "owned private evidence"):
                            transport.HostTransport("/synthetic/ssh", source, source,
                                                    "ecdsa-sha2-nistp256 AAAAtest fixture", evidence)
                        temporary.assert_not_called()
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["link", "shared", "synthetic-key"])

    def test_remote_negative_string_remains_one_argument_and_no_ambient_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.client(directory)
            remote = "status ryxos-lifecycle; touch /tmp/escape"
            result = subprocess.CompletedProcess([], 64, "Only status, start, stop or reset")
            with patch.object(transport.subprocess, "run", return_value=result) as run:
                code, _ = client.call(remote)
            self.assertEqual(code, 64)
            args, kwargs = run.call_args
            self.assertEqual(args[0][-2:], [transport.TARGET, remote])
            self.assertEqual(args[0][1:3], ["-F", "/dev/null"])
            self.assertIn("IdentityAgent=none", args[0])
            self.assertNotIn("SSH_AUTH_SOCK", kwargs["env"])
            self.assertNotIn("shell", kwargs)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_arbitrary_key_and_unbounded_timeout_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.client(directory)
            for options in ({"key": "/home/operator/.ssh/id_ed25519"}, {"timeout": 181}):
                with self.subTest(options=options), patch.object(transport.subprocess, "run") as run:
                    with self.assertRaises(AssertionError):
                        client.call("status ryxos-lifecycle", **options)
                    run.assert_not_called()

    def test_forwarding_verbosity_is_explicit_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.client(directory)
            result = subprocess.CompletedProcess([], 255, "administratively prohibited")
            with patch.object(transport.subprocess, "run", return_value=result) as run:
                code, output = client.call(options=["-T", "-W", "127.0.0.1:22222"], log_level="DEBUG1")
            self.assertEqual(code, 255)
            self.assertIn("administratively prohibited", output)
            self.assertIn("LogLevel=DEBUG1", run.call_args.args[0])
            with patch.object(transport.subprocess, "run") as run:
                with self.assertRaises(AssertionError):
                    client.call("status ryxos-lifecycle", log_level="arbitrary")
                run.assert_not_called()

    def test_timeout_is_a_failure_not_a_successful_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.client(directory)
            with patch.object(transport.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 20)):
                with self.assertRaises(subprocess.TimeoutExpired):
                    client.call("status ryxos-lifecycle")

    def finished_receipt(self):
        return {"status": "passed", "graceful_cleanup": True, "dom0_shutdown": True,
                "ssh_control": {"status": "passed", "host_network_connection": True,
                                "listener_stopped": True, "operator_host_route_qualified": False}}

    def test_qualification_waits_for_successful_shutdown_and_transport_cleanup(self):
        receipt = self.finished_receipt()
        client = object.__new__(transport.HostTransport)

        def finish():
            self.assertFalse(receipt["ssh_control"]["operator_host_route_qualified"])
            return {"host_listener_gone": True, "fixture_keys_removed": True}

        client.finish = Mock(side_effect=finish)
        client.finish_receipt(receipt)
        client.finish.assert_called_once_with()
        self.assertTrue(receipt["ssh_control"]["operator_host_route_qualified"])

    def test_cleanup_failure_or_interruption_cannot_leave_qualified_claim(self):
        for error in (OSError("fixture key cleanup failed"), AssertionError("listener remains"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                receipt = self.finished_receipt()
                receipt["ssh_control"]["operator_host_route_qualified"] = True
                client = object.__new__(transport.HostTransport)
                client.finish = Mock(side_effect=error)
                client.finish_receipt(receipt)
                self.assertEqual(receipt["status"], "failed")
                self.assertFalse(receipt["ssh_control"]["operator_host_route_qualified"])
                self.assertIn("error", receipt["host_transport_cleanup"])

    def test_missing_or_failed_earlier_evidence_remains_unqualified(self):
        fields = ((None, "status"), (None, "graceful_cleanup"), (None, "dom0_shutdown"),
                  ("ssh_control", "status"), ("ssh_control", "host_network_connection"),
                  ("ssh_control", "listener_stopped"))
        for section, field in fields:
            for value in (None, False, "failed"):
                with self.subTest(section=section, field=field, value=value):
                    receipt = self.finished_receipt()
                    (receipt if section is None else receipt[section])[field] = value
                    client = object.__new__(transport.HostTransport)
                    client.finish = Mock(return_value={"host_listener_gone": True, "fixture_keys_removed": True})
                    client.finish_receipt(receipt)
                    self.assertFalse(receipt["ssh_control"]["operator_host_route_qualified"])
        for key in ("host_listener_gone", "fixture_keys_removed"):
            with self.subTest(cleanup=key):
                receipt = self.finished_receipt()
                cleanup = {"host_listener_gone": True, "fixture_keys_removed": True}
                cleanup[key] = False
                client = object.__new__(transport.HostTransport)
                client.finish = Mock(return_value=cleanup)
                client.finish_receipt(receipt)
                self.assertFalse(receipt["ssh_control"]["operator_host_route_qualified"])

    def test_cleanup_removes_only_private_material_even_if_listener_remains(self):
        client = object.__new__(transport.HostTransport)
        client.temporary = tempfile.TemporaryDirectory()
        path = Path(client.temporary.name)
        (path / "synthetic-key").write_text("public fixture bytes")
        with patch.object(transport, "listener_rows", return_value=[{"inode": "918"}]):
            with self.assertRaisesRegex(AssertionError, "remains"):
                client.finish()
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
