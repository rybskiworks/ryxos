import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "modules" / "xen-control.py"
MODULE_SPEC = importlib.util.spec_from_file_location("xen_control", SOURCE)
control = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(control)


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.policy = {
            "schema": 1,
            "helper": "/nix/store/00000000000000000000000000000000-helper/bin/ryxos-xen-domain",
            "domains": ["branch", "Leaf_2"],
        }
        self.events = []
        self.calls = []
        self.stderr = io.StringIO()

    def fake_run(self, arguments, **kwargs):
        self.calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0)

    def dispatch(self, command, **environment):
        return control.dispatch(self.policy, environment | {"SSH_ORIGINAL_COMMAND": command},
                                run=self.fake_run, record=self.events.append, stderr=self.stderr)

    def test_all_allowed_actions_use_exact_fixed_argv(self):
        for action in control.ACTIONS:
            with self.subTest(action=action):
                self.assertEqual(self.dispatch(f"{action} branch"), 0)
                arguments, options = self.calls[-1]
                self.assertEqual(arguments, [control.SUDO, "-n", "-u", "root", "--",
                                             self.policy["helper"], action, "branch"])
                self.assertFalse(options["shell"])
                self.assertEqual(options["stdin"], subprocess.DEVNULL)
                self.assertEqual(options["cwd"], "/")
                self.assertEqual(options["timeout"], 900)
        self.assertEqual(self.dispatch("status Leaf_2"), 0)

    def test_parser_rejects_shells_options_traversal_and_noncanonical_text(self):
        invalid = [
            None, "", "status", "list", "list branch", "force-stop branch", "restart branch",
            "status other", "status Branch", "status /branch", "status ../branch", "status --spec",
            "status branch extra", " status branch", "status branch ", "status  branch",
            "status\tbranch", "status branch\n", "status branch\r", "status branch\x00",
            "status branch; id", "status $(id)", "status `id`", "status 'branch'",
            'status "branch"', "status branch && id", "status branch | id", "status *",
            "status bránch", "ｓtatus branch", "STATUS branch", "status branch/../../etc",
            "SSH_ORIGINAL_COMMAND=status branch", "sudo status branch", "status " + "a" * 256,
        ]
        for command in invalid:
            with self.subTest(command=repr(command)):
                self.assertEqual(self.dispatch(command), 64)
        self.assertEqual(self.calls, [])
        for event in self.events:
            self.assertEqual(set(event), {"account", "peer", "result", "exit_code"})
            self.assertEqual(event["result"], "refused")

    def test_environment_and_remote_input_never_reach_privileged_helper(self):
        self.assertEqual(self.dispatch(
            "start branch", PATH="/tmp/attacker", BASH_ENV="/tmp/startup", PYTHONPATH="/tmp/python",
            LD_PRELOAD="/tmp/library.so", NIX_CONFIG="builders = attacker", SSH_AUTH_SOCK="/tmp/agent",
            SUDO_ASKPASS="/tmp/askpass", SECRET="synthetic-never-forward",
            SSH_CONNECTION="192.0.2.10 4567 192.0.2.20 22",
        ), 0)
        self.assertEqual(self.calls[0][1]["env"], control.CHILD_ENV)
        self.assertNotIn("synthetic-never-forward", json.dumps(self.events))
        self.assertEqual(self.events[0]["peer"], "192.0.2.10")
        self.assertEqual(self.events[-1]["result"], "completed")

    def test_child_environment_is_a_new_mapping_per_call(self):
        self.dispatch("status branch")
        self.calls[0][1]["env"]["UNEXPECTED"] = "value"
        self.dispatch("status branch")
        self.assertNotIn("UNEXPECTED", self.calls[1][1]["env"])

    def test_nonzero_and_signal_exit_are_reported_without_retry(self):
        for code, expected in ((7, 7), (-15, 1)):
            calls = []
            def failed(arguments, **kwargs):
                calls.append(arguments)
                return subprocess.CompletedProcess(arguments, code)
            result = control.dispatch(self.policy, {"SSH_ORIGINAL_COMMAND": "stop branch"},
                                      run=failed, record=self.events.append, stderr=self.stderr)
            self.assertEqual(result, expected)
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.events[-1]["exit_code"], expected)
            self.assertEqual(self.events[-1]["result"], "failed")

    def test_timeout_and_unavailable_do_not_issue_force_or_retry(self):
        for error, expected, status in (
            (subprocess.TimeoutExpired("synthetic", 900), 124, "timeout"),
            (FileNotFoundError("synthetic private path"), 69, "unavailable"),
        ):
            calls = []
            def failed(arguments, **kwargs):
                calls.append(arguments)
                raise error
            result = control.dispatch(self.policy, {"SSH_ORIGINAL_COMMAND": "start branch"},
                                      run=failed, record=self.events.append, stderr=self.stderr)
            self.assertEqual(result, expected)
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.events[-1]["result"], status)
            self.assertNotIn("synthetic private path", self.stderr.getvalue())
            self.assertNotIn("force-stop", calls[0])

    def test_audit_peer_is_validated_and_never_raw(self):
        self.assertEqual(control.peer_address({"SSH_CONNECTION": "2001:db8::1 25 2001:db8::2 22"}), "2001:db8::1")
        for value in ("", "192.0.2.1 22", "192.0.2.1 0 192.0.2.2 22", "192.0.2.1 70000 192.0.2.2 22",
                      "message\ninjection 22 192.0.2.2 22", "192.0.2.1 22 invalid 22"):
            self.assertEqual(control.peer_address({"SSH_CONNECTION": value}), "unknown")

    def test_immutable_policy_admission_and_malformed_policy_refusals(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory)
            path = store / "policy.json"
            policy = self.policy | {"helper": str(store / "00000000000000000000000000000000-helper/bin/ryxos-xen-domain")}
            def save(value):
                if path.exists():
                    path.chmod(0o600)
                path.write_text(json.dumps(value))
                path.chmod(0o444)
            save(policy)
            self.assertEqual(control.policy_from_path(path, store), policy)
            for changed in (
                policy | {"schema": 2}, policy | {"unknown": "value"},
                policy | {"domains": []}, policy | {"domains": ["branch", "branch"]},
                policy | {"domains": ["../branch"]}, policy | {"domains": [1]},
                policy | {"helper": "/tmp/helper"}, policy | {"helper": policy["helper"] + " --spec /tmp/other"},
            ):
                save(changed)
                with self.assertRaises(control.Refused):
                    control.policy_from_path(path, store)
            save(policy)
            path.chmod(0o644)
            with self.assertRaises(control.Refused):
                control.policy_from_path(path, store)
            path.chmod(0o444)
            alias = store / "alias.json"
            alias.symlink_to(path)
            with self.assertRaises(control.Refused):
                control.policy_from_path(alias, store)
            nested = store / "nested"
            nested.mkdir()
            with self.assertRaises(control.Refused):
                control.policy_from_path(nested / "policy.json", store)

    def test_policy_size_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_bytes(b" " * (control.MAX_POLICY_BYTES + 1))
            path.chmod(0o444)
            with self.assertRaises(control.Refused):
                control.policy_from_path(path, Path(directory))

    def test_main_rejects_extra_arguments_before_reading_policy(self):
        with patch.object(control, "policy_from_path") as load, patch.object(control, "audit") as record:
            with contextlib.redirect_stderr(self.stderr):
                self.assertEqual(control.main(["/nix/store/policy", "--spec", "/tmp/other"]), 78)
            load.assert_not_called()
            self.assertEqual(record.call_args.args[0]["result"], "invalid_policy")

    def test_syslog_records_only_structured_event(self):
        event = {"account": control.ACCOUNT, "result": "refused", "exit_code": 64}
        with patch.object(control.syslog, "openlog") as opened, patch.object(control.syslog, "syslog") as logged:
            control.audit(event)
            self.assertEqual(opened.call_args.args, ("ryxos-xen-control", control.syslog.LOG_PID, control.syslog.LOG_AUTHPRIV))
            self.assertEqual(json.loads(logged.call_args.args[1]), event)


if __name__ == "__main__":
    unittest.main()
