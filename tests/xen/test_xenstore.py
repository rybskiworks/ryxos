import unittest

from xenstore import check


class Machine:
    def __init__(self, journal="Starting xenstored\n", parser_fails=False, matrix_fails=False):
        self.journal = journal
        self.parser_fails = parser_fails
        self.matrix_fails = matrix_fails
        self.commands = []

    def succeed(self, command, *, timeout):
        self.commands.append(command)
        if "check_xenstore_config.py" in command and self.matrix_fails:
            raise RuntimeError("parser matrix failed")
        if "--config-test" in command:
            if self.parser_fails:
                raise RuntimeError("strict parser rejected configuration")
            return "Configuration valid\n"
        if command.startswith("journalctl -b "):
            return self.journal
        if command.startswith("sha256sum "):
            return "a" * 64 + "  /etc/xen/oxenstored.conf\n"
        if command.startswith("readlink -f "):
            return "/nix/store/example-oxenstored.conf\n"
        return ""


class XenstoreStartupTests(unittest.TestCase):
    def test_records_config_identity_after_parser_and_journal_pass(self):
        result = check(Machine(), "/nix/store/xen/bin/oxenstored")
        self.assertTrue(result["strict_parser_passed"])
        self.assertTrue(result["parser_matrix_passed"])
        self.assertEqual(result["config_sha256"], "a" * 64)
        self.assertEqual(result["startup_config_errors"], [])

    def test_parser_failure_stops_before_startup_acceptance(self):
        machine = Machine(parser_fails=True)
        with self.assertRaisesRegex(RuntimeError, "strict parser"):
            check(machine, "/nix/store/xen/bin/oxenstored")
        self.assertEqual(len(machine.commands), 1)

    def test_boolean_and_unknown_key_warnings_are_failures(self):
        for warning in (
            "config: quota-activate: expect bool arg",
            "config: unknown key acesss-log-nb-chars",
        ):
            with self.subTest(warning=warning), self.assertRaisesRegex(AssertionError, "configuration errors"):
                check(Machine(warning + "\n"), "/nix/store/xen/bin/oxenstored")

    def test_matrix_failure_stops_before_startup_acceptance(self):
        machine = Machine(matrix_fails=True)
        with self.assertRaisesRegex(RuntimeError, "parser matrix failed"):
            check(machine, "/nix/store/xen/bin/oxenstored")
        self.assertEqual(len(machine.commands), 2)

    def test_optional_dom0_uuid_notice_is_not_a_config_error(self):
        result = check(Machine("XEN_DOM0_UUID is not set\n"), "/nix/store/xen/bin/oxenstored")
        self.assertTrue(result["strict_parser_passed"])

    def test_oversized_journal_refuses_truncated_acceptance(self):
        with self.assertRaisesRegex(AssertionError, "evidence bound"):
            check(Machine("x" * 131073), "/nix/store/xen/bin/oxenstored")


if __name__ == "__main__":
    unittest.main()
