"""Check the installed OCaml configuration and actual Xenstore startup log."""

import re
import shlex


def check(machine, parser):
    config = "/etc/xen/oxenstored.conf"
    machine.succeed(shlex.quote(parser) + " --config-test --config-file " + config, timeout=10)
    machine.succeed(
        "python3 -B /etc/ryxos-test/check_xenstore_config.py --parser "
        + shlex.quote(parser)
        + " --fixtures /etc/ryxos-test/xenstore-config-fixtures.json",
        timeout=60,
    )
    machine.succeed("journalctl --sync", timeout=10)
    journal = machine.succeed("journalctl -b -u xenstored.service --no-pager -o cat", timeout=10)
    if len(journal) > 131072:
        raise AssertionError("Xenstore startup journal exceeds the evidence bound")
    errors = [line for line in journal.splitlines() if "config:" in line]
    if errors:
        raise AssertionError("Xenstore configuration errors: " + repr(errors))
    digest = machine.succeed("sha256sum " + config, timeout=10).split()[0]
    source = machine.succeed("readlink -f " + config, timeout=10).strip()
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or not source.startswith("/nix/store/"):
        raise AssertionError("Invalid Xenstore configuration identity")
    return {
        "parser": parser,
        "config_source": source,
        "config_sha256": digest,
        "strict_parser_passed": True,
        "parser_matrix_passed": True,
        "startup_config_errors": [],
    }
