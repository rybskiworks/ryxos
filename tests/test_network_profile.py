from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock, patch

from test_lab_supervisor import FilesystemTest, lab


class NetworkProfileTests(FilesystemTest):
    def driver(self, change=lambda value: value, names=("dom0",)):
        store = self.root / "store"
        store.mkdir(exist_ok=True)
        package = store / ("a" * 32 + "-profile-" + str(len(list(store.iterdir()))))
        package.mkdir()
        driver, config, launcher = (package / name for name in ("driver", "config.json", "launcher"))
        driver.write_text(f"#!/bin/sh\nexec /nix/store/fake/bin/driver --config {config}\n")
        config.write_text(json.dumps({"vms": {name: {"start_script": str(launcher)} for name in names},
                                      "containers": {}, "vlans": [], "enable_ssh_backdoor": False}))
        source = """exec /nix/store/fake/bin/qemu-system-x86_64 -machine accel=kvm -cpu max \\
  -name dom0 \\
  -m 6144 \\
  -smp 4 \\
  -device virtio-rng-pci \\
  -nic none \\
  -netdev user,id=ssh0,restrict=on,ipv6=off,hostfwd=tcp:127.0.0.1:22222-10.0.2.15:22222 \\
  -device virtio-net-pci,netdev=ssh0,mac=52:54:00:72:78:01 \\
  -drive cache=writeback,file="$NIX_DISK_IMAGE",id=drive1,if=none,index=1,werror=report \\
  -device virtio-blk-pci,bootindex=1,drive=drive1,serial=root \\
  -cpu host \\
  -machine q35 \\
  -no-user-config \\
  -device virtio-keyboard \\
  -usb \\
  -device usb-tablet,bus=usb-bus.0 \\
  -object memory-backend-memfd,id=mem0,size=6144M,share=on \\
  -machine memory-backend=mem0 \\
  -drive if=pflash,format=raw,unit=0,readonly=on,file=/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-OVMF-202602-fd/FV/OVMF_CODE.fd \\
  -drive if=pflash,format=raw,unit=1,readonly=off,file=$NIX_EFI_VARS \\
  -nographic \\
  $QEMU_OPTS \\
  "$@"
"""
        launcher.write_text(change(source))
        for path in (driver, launcher):
            path.chmod(0o555)
        config.chmod(0o444)
        return store, driver

    def inspect(self, change=lambda value: value, names=("dom0",)):
        store, driver = self.driver(change, names)
        return lab.inspect_driver(driver, 6144, 4, store, network_profile="loopback-ssh")

    def test_exact_opt_in_profile_is_admitted_and_recorded(self):
        record = self.inspect()
        self.assertEqual(record["network_policy"], lab.network_policy("loopback-ssh"))
        self.assertEqual(record["machines"][0]["memory_mib"], 6144)
        forward = record["network_policy"]["host_forwards"][0]
        self.assertEqual((forward["host_address"], forward["host_port"]), ("127.0.0.1", 22222))

    def test_default_still_refuses_explicit_network(self):
        store, driver = self.driver()
        with self.assertRaises(lab.Refusal):
            lab.inspect_driver(driver, 6144, 4, store)

    def test_closed_launcher_remains_admitted_only_as_closed(self):
        def closed(source):
            return "\n".join(line for line in source.splitlines()
                              if "-netdev" not in line and "-device virtio-net-" not in line) + "\n"
        store, driver = self.driver(closed)
        record = lab.inspect_driver(driver, 6144, 4, store)
        self.assertEqual(record["network_policy"], {"profile": "closed", "host_forwards": []})
        with self.assertRaises(lab.Refusal):
            lab.inspect_driver(driver, 6144, 4, store, network_profile="loopback-ssh")

    def test_noncanonical_bind_port_mac_restriction_and_forwarding_refused(self):
        changes = [
            ("127.0.0.1:22222-", "0.0.0.0:22222-"),
            ("127.0.0.1:22222-", ":22222-"),
            ("127.0.0.1:22222-", "[::1]:22222-"),
            ("127.0.0.1:22222-", "127.0.0.1:22223-"),
            ("10.0.2.15:22222", "10.0.2.16:22222"),
            ("10.0.2.15:22222", "10.0.2.15:22"),
            ("52:54:00:72:78:01", "52:54:00:72:78:02"),
            ("restrict=on", "restrict=off"), ("restrict=on,", ""),
            ("ipv6=off", "ipv6=on"), ("hostfwd=tcp", "hostfwd=udp"),
            ("hostfwd=tcp", "guestfwd=tcp"),
            ("user,id=ssh0,", "user,id=ssh0,tftp=/tmp,"),
            ("user,id=ssh0,", "user,id=ssh0,smb=/tmp,"),
            ("-netdev user,", "-netdev=user,"),
        ]
        for old, new in changes:
            with self.subTest(old=old, new=new), self.assertRaises(lab.Refusal):
                self.inspect(lambda source: source.replace(old, new))

    def test_duplicate_and_extra_network_devices_refused(self):
        for extra in ("-nic none", "-nic user", "-netdev " + lab.SSH_NETDEV,
                      "-device " + lab.SSH_DEVICE, "-device e1000", "-device rtl8139",
                      "-net user", "-device vfio-pci,host=00:01.0",
                      "-device virtio-9p-pci,fsdev=escape", "-device vhost-user-fs-pci,chardev=escape"):
            with self.subTest(extra=extra), self.assertRaises(lab.Refusal):
                self.inspect(lambda source: source.replace("  $QEMU_OPTS", "  " + extra + "\n  $QEMU_OPTS"))

    def test_paths_extra_arguments_and_overrides_refused(self):
        for extra in ("-readconfig /tmp/override", "-set netdev.ssh0.restrict=off",
                      "-global driver=escape", "-chardev socket,id=escape,host=127.0.0.1,port=1",
                      "-monitor tcp:127.0.0.1:1", "-drive file=/dev/physical,if=virtio",
                      "-virtfs local,path=/home,mount_tag=escape", "-fsdev local,id=escape,path=/home",
                      "--netdev " + lab.SSH_NETDEV, "$QEMU_NET_OPTS", "-accel tcg", "''", "' '"):
            with self.subTest(extra=extra), self.assertRaises(lab.Refusal):
                self.inspect(lambda source: source.replace("  $QEMU_OPTS", "  " + extra + "\n  $QEMU_OPTS"))

    def test_fixed_disk_and_firmware_paths_cannot_be_replaced(self):
        for old, new in (("$NIX_DISK_IMAGE", "/dev/physical"), ("$NIX_EFI_VARS", "/important"),
                         ("readonly=on", "readonly=off"), ("/FV/OVMF_CODE.fd", "/../../etc/passwd")):
            with self.subTest(old=old), self.assertRaises(lab.Refusal):
                self.inspect(lambda source: source.replace(old, new))

    def test_incomplete_args_and_environment_assignment_refused(self):
        for change in (lambda source: source.replace("  $QEMU_OPTS", "  -netdev\n  $QEMU_OPTS"),
                       lambda source: "QEMU_OPTS='-net user'\n" + source,
                       lambda source: source.replace('"$@"', '"$EXTRA"')):
            with self.subTest(change=change), self.assertRaises(lab.Refusal):
                self.inspect(change)

    def test_multiple_or_differently_named_machines_refused(self):
        for names in (("guest",), ("dom0", "other")):
            with self.subTest(names=names), self.assertRaises(lab.Refusal):
                self.inspect(names=names)

    def test_legacy_policy_and_exact_recorded_mapping(self):
        self.assertEqual(lab.request_network_policy({}), lab.network_policy("closed"))
        policy = lab.network_policy("loopback-ssh")
        request = {"network_policy": policy, "driver_admission": {"network_policy": policy}}
        self.assertEqual(lab.request_network_policy(request), policy)
        for changed in ({"profile": "unknown"}, {"profile": "closed", "host_forwards": [{}]},
                        {"profile": "loopback-ssh", "host_forwards": []}, None):
            if changed is None:
                value = {"network_policy": policy}
            else:
                value = {"network_policy": changed, "driver_admission": {"network_policy": changed}}
            with self.subTest(value=value), self.assertRaises(lab.Refusal):
                lab.request_network_policy(value)
        for request in ({"network_policy": None},
                        {"network_policy": lab.network_policy("closed"), "driver_admission": {"network_policy": policy}},
                        {"driver_admission": {"network_policy": policy}}):
            with self.subTest(request=request), self.assertRaises(lab.Refusal):
                lab.request_network_policy(request)

    def prepare_inside(self):
        request = self.request()
        request["limits"].update(memory_mib=6144, vcpus=4)
        request["network_policy"] = lab.network_policy("loopback-ssh")
        request["driver"] = "/unexecuted/synthetic-driver"
        admitted = {"driver": request["driver"], "network_policy": request["network_policy"],
                    "machines": [{"sha256": "synthetic-immutable-content"}]}
        request["driver_admission"] = admitted
        output = Path(request["run_dir"])
        lab.write_json(output / "request.json", request)
        return request, output, admitted

    def test_inside_rechecks_the_exact_recorded_driver_before_execution(self):
        request, output, admitted = self.prepare_inside()
        run = Mock(return_value=subprocess.CompletedProcess([], 0))
        with patch.object(lab, "inspect_driver", return_value=admitted) as inspect:
            self.assertEqual(lab.inside(output / "request.json", verify=lambda _: {}, run=run), 0)
        inspect.assert_called_once_with(request["driver"], 6144, 4, network_profile="loopback-ssh")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(lab.read_json(output / "inside.json")["network_policy"], request["network_policy"])

    def test_changed_admission_blocks_inside_without_starting_driver(self):
        _, output, admitted = self.prepare_inside()
        changed = dict(admitted, machines=[{"sha256": "different"}])
        run = Mock()
        with patch.object(lab, "inspect_driver", return_value=changed):
            self.assertEqual(lab.inside(output / "request.json", verify=lambda _: {}, run=run), 1)
        run.assert_not_called()
        self.assertFalse(lab.read_json(output / "inside.json")["driver_started"])

    def test_recovery_and_inside_cannot_override_recorded_profile(self):
        for operation in ("--recover", "--inside"):
            for profile in ("closed", "loopback-ssh"):
                with self.subTest(operation=operation, profile=profile), redirect_stderr(io.StringIO()):
                    with patch.object(lab, "executable") as executable:
                        with self.assertRaises(SystemExit) as error:
                            lab.main([operation, "/unused", "--network-profile", profile])
                        self.assertEqual(error.exception.code, 2)
                        executable.assert_not_called()

    def test_cli_default_and_opt_in_do_not_add_arbitrary_options(self):
        for selection in ([], ["--network-profile", "loopback-ssh"]):
            with self.subTest(selection=selection), redirect_stdout(io.StringIO()):
                with patch.object(lab, "executable", return_value="/fixed/tool"), patch.object(lab, "execute", return_value=({}, 0)) as execute:
                    self.assertEqual(lab.main(["--driver", "/unused", "--output-dir", "/unused"] + selection), 0)
                    self.assertEqual(execute.call_args.args[0].network_profile, "loopback-ssh" if selection else None)
        for extra in (["--host-port", "22223"], ["--network-profile", "arbitrary"]):
            with self.subTest(extra=extra), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                lab.main(["--driver", "/unused", "--output-dir", "/unused"] + extra)
