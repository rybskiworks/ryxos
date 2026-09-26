{
  nixpkgs,
  pkgs,
  baseModule,
  xenDomainsModule,
  xenControlModule,
}:
let
  inherit (pkgs) lib;
  domainName = "ryxos-lifecycle";
  domainUuid = "adf2c635-e41c-477e-a3ad-ef936084437d";
  control = import ./control.nix {
    inherit pkgs domainName xenControlModule;
  };
  guest = nixpkgs.lib.nixosSystem {
    system = pkgs.stdenv.hostPlatform.system;
    modules = [
      baseModule
      ./lifecycle-guest.nix
      { nixpkgs.pkgs = pkgs; }
    ];
  };
  rootImage = import (nixpkgs + "/nixos/lib/make-disk-image.nix") {
    inherit pkgs lib;
    inherit (guest) config;
    name = "xen-lifecycle-root";
    baseName = "xen-lifecycle";
    label = "ryxos-lifecycle";
    format = "raw";
    partitionTableType = "none";
    installBootLoader = false;
    copyChannel = false;
    diskSize = "auto";
    additionalSpace = "128M";
  };
  backing = "${rootImage}/xen-lifecycle.img";
  mkDomainConfig =
    memory:
    pkgs.writeText "xen-lifecycle-${toString memory}.cfg" ''
      name = "${domainName}"
      uuid = "${domainUuid}"
      type = "hvm"
      memory = ${toString memory}
      maxmem = ${toString memory}
      vcpus = 2
      hap = 1
      nestedhvm = 0
      altp2m = 0
      kernel = "${guest.config.system.build.kernel}/${guest.config.system.boot.loader.kernelFile}"
      ramdisk = "${guest.config.system.build.initialRamdisk}/${guest.config.system.boot.loader.initrdFile}"
      extra = ${
        builtins.toJSON (
          lib.concatStringsSep " " (
            [ "init=/nix/var/nix/profiles/system/init" ] ++ guest.config.boot.kernelParams
          )
        )
      }
      device_model_args_hvm = [ "-machine", "xenfv-4.2,suppress-vmdesc=on" ]
      disk = [ ]
      vif = [ ]
      on_poweroff = "destroy"
      on_reboot = "destroy"
      on_crash = "destroy"
    '';
  test = pkgs.testers.runNixOSTest {
    name = "xen-managed-lifecycle";
    globalTimeout = 1200;
    qemu.forceAccel = true;
    # Release the reviewed builder's staging copy before raw-to-qcow2 conversion.
    # This affects only the test node's image constructor, not its system closure.
    node.pkgs = lib.mkForce (import ./staging-pkgs.nix { inherit pkgs; });
    nodes.dom0 = {
      imports = [
        baseModule
        ./dom0.nix
        xenDomainsModule
        control.module
      ];
      # The backing is an actual store file in this self-contained dom0 image.
      # QEMU's sparse outer disk and the managed qcow2 remain disposable.
      virtualisation.diskSize = 12288;
      virtualisation.fileSystems."/".autoResize = true;
      boot.growPartition = true;
      boot.kernelModules = [ "nbd" ];
      boot.extraModprobeConfig = ''
        options nbd nbds_max=1 max_part=0
      '';
      systemd.services.xenconsoled.environment = {
        XENCONSOLED_TRACE = "guest";
        XENCONSOLED_LOG_DIR = "/var/log/ryxos-lifecycle-console";
      };
      ryxos.xenDomains = {
        enable = true;
        domains.${domainName} = {
          uuid = domainUuid;
          configFile = mkDomainConfig 2048;
          disposableImage = backing;
          startTimeoutSec = 120;
          shutdownTimeoutSec = 90;
        };
      };
      specialisation.changed.configuration = {
        ryxos.xenDomains.domains.${domainName}.configFile = lib.mkForce (mkDomainConfig 1792);
      };
    };
    testScript =
      { nodes, ... }:
      let
        system = nodes.dom0.system.build.toplevel;
      in
      ''
        import json
        import os
        import shlex
        import time
        import uuid
        from pathlib import Path
        from typing import Any

        name = "${domainName}"
        domain_uuid = "${domainUuid}"
        helper = "/run/current-system/sw/bin/ryxos-xen-domain"
        unit = "ryxos-xen-" + name + ".service"
        state_dir = "/var/lib/ryxos-xen-domains/" + name
        overlay = state_dir + "/disk.qcow2"
        lease = "/nix/var/nix/gcroots/ryxos-xen-domains/" + name
        console_dir = "/var/log/ryxos-lifecycle-console"
        qemu_img = "${nodes.dom0.virtualisation.xen.qemu.package}/bin/qemu-img"
        qemu_nbd = "${nodes.dom0.virtualisation.xen.qemu.package}/bin/qemu-nbd"
        receipt: dict[str, Any] = {
            "schema": 1, "status": "running", "name": name, "uuid": domain_uuid,
            "nixpkgs_revision": "${nixpkgs.rev}", "xen_version": "${pkgs.xen.version}",
            "guest_type": "hvm", "nestedhvm": False, "host_shares": False,
            "external_network": False, "production_credentials": False,
            "synthetic_ssh_fixture": True, "physical_host_qualified": False,
        }
        started = False

        def command(action):
            return helper + " " + action + " " + name

        def inventory():
            plain = dom0.succeed("xl list").strip().splitlines()
            assert plain and plain[0].startswith("Name"), plain
            ids = {int(line.split()[1]) for line in plain[1:]}
            details = json.loads(dom0.succeed("xl list --long"))
            assert isinstance(details, list), details
            rows = []
            for item in details:
                if item["domid"] != 0:
                    identity = item["config"]["c_info"]
                    rows.append({"domid": item["domid"], "name": identity["name"],
                                 "uuid": str(uuid.UUID(identity["uuid"]))})
            assert ids == {0} | {item["domid"] for item in rows}, (plain, details)
            assert len(rows) == len(ids) - 1, rows
            return rows

        def status():
            return json.loads(dom0.succeed(command("status")))

        def state():
            return json.loads(dom0.succeed("cat " + state_dir + "/state.json"))

        def owned():
            rows, observed = inventory(), status()
            assert len(rows) == 1, rows
            assert rows[0]["name"] == name and rows[0]["uuid"] == domain_uuid, rows
            assert observed["owned"] and observed["status"] == "present", observed
            assert observed["domid"] == rows[0]["domid"], (observed, rows)
            return observed

        def ready(previous_ids):
            deadline = time.monotonic() + 150
            prefix = "RYXOS_XEN_LIFECYCLE_READY "
            while time.monotonic() < deadline:
                _, raw = dom0.execute("tail -c 131072 " + console_dir + "/*.log 2>/dev/null", timeout=10)
                for line in reversed(raw.splitlines()):
                    if prefix not in line:
                        continue
                    try:
                        result = json.loads(line.split(prefix, 1)[1])
                    except json.JSONDecodeError:
                        continue
                    if result["guest_boot_id"] in previous_ids:
                        continue
                    assert result["status"] == "passed", result
                    assert str(uuid.UUID(result["guest_boot_id"])) == result["guest_boot_id"], result
                    assert result["command_output"] == "RYXOS_XEN_LIFECYCLE_OK", result
                    assert result["command_exit_code"] == 0 and result["network_interfaces"] == ["lo"], result
                    assert result["egress_probe"]["status"] == "denied", result
                    assert result["egress_probe"]["attempted"] and result["egress_probe"]["errno"] == 101, result
                    return result
                time.sleep(1)
            raise AssertionError("The real guest did not emit its fixed serial readiness receipt")

        def disk_receipt(expected):
            assert inventory() == [], "Never attach a live guest disk to an inspection device"
            info = json.loads(dom0.succeed(qemu_img + " info --output=json " + overlay))
            assert info["format"] == "qcow2" and info["backing-filename-format"] == "raw", info
            assert info["full-backing-filename"] == "${backing}", info
            dom0.succeed("test -b /dev/nbd0 && test ! -e /sys/class/block/nbd0/pid")
            attached = False
            try:
                dom0.succeed(qemu_nbd + " --read-only --connect=/dev/nbd0 " + overlay, timeout=30)
                attached = True
                dom0.succeed("test $(blockdev --getro /dev/nbd0) = 1")
                text = dom0.succeed("debugfs -R 'cat /var/lib/ryxos-lifecycle-probe/result.json' /dev/nbd0 2>/dev/null")
                assert json.loads(text) == expected, text
            finally:
                if attached:
                    dom0.succeed(qemu_nbd + " --disconnect /dev/nbd0", timeout=30)
                    dom0.wait_until_succeeds("test ! -e /sys/class/block/nbd0/pid", timeout=15)
            return info

        def stop_and_check(expected):
            dom0.succeed("systemctl stop " + unit, timeout=120)
            result = dom0.succeed("systemctl show --property=Result --value " + unit).strip()
            assert result == "success", "The stop job finished but its service failed: " + result
            assert inventory() == [], "Graceful stop left a domain present"
            dom0.wait_until_succeeds(
                "group=$(systemctl show --property=ControlGroup --value " + unit + "); "
                "test -z \"$group\" || test ! -e \"/sys/fs/cgroup$group/cgroup.events\" || "
                "grep -Fx 'populated 0' \"/sys/fs/cgroup$group/cgroup.events\"", timeout=15)
            observed, retained = status(), state()
            assert observed["status"] == "absent" and not observed["owned"], observed
            assert retained["phase"] == "stopped", retained
            assert dom0.succeed("readlink " + lease).strip() == retained["specPath"], retained
            return {"state": retained, "disk": disk_receipt(expected), "helper_cgroup_empty": True}

        try:
            launcher = Path("${nodes.dom0.system.build.vm}/bin/run-${nodes.dom0.networking.hostName}-vm").read_text()
            assert "-virtfs" not in launcher and "-fsdev" not in launcher, "No host filesystem sharing"
            assert "-nic none" in launcher and "-no-user-config" in launcher, "Explicit outer isolation is missing"
            dom0.start()
            started = True
            dom0.wait_for_unit("multi-user.target")
            dom0.wait_for_unit("xenstored.service")
            dom0.wait_for_unit("xenconsoled.service")
            dom0.succeed("grep -Fx control_d /proc/xen/capabilities")
            dom0.succeed("test $(ls /sys/class/net | wc -l) = 1 && test -d /sys/class/net/lo")
            dom0.succeed("nix-store --verify --check-contents", timeout=180)
            assert inventory() == [], "The test must start with only domain zero"
            initial_spec = dom0.succeed("readlink -f /etc/ryxos/xen-domains/" + name + ".json").strip()
            declaration = json.loads(dom0.succeed("cat " + shlex.quote(initial_spec)))
            receipt["parsed_xl_config"] = json.loads(dom0.succeed(
                "xl create --dryrun " + shlex.quote(declaration["configFile"])))
            assert status()["retained_spec"] is None
            dom0.succeed("systemctl start " + unit, timeout=150)
            first = ready(set())
            first_state, first_status = state(), owned()
            assert first["boot_count"] == 1 and first["marker_created"], first
            assert first_status["retained_spec"] == initial_spec, first_status
            dom0.fail(command("start"))
            dom0.fail(command("reset"))
            assert owned()["domid"] == first_status["domid"], "Refusal must preserve the running domain"
            assert state()["diskIdentity"] == first_state["diskIdentity"]
            receipt["first_boot"] = first
            receipt["duplicate_and_live_reset_refused"] = True

            dom0.succeed("${system}/specialisation/changed/bin/switch-to-configuration switch", timeout=180)
            changed = owned()
            assert changed["domid"] == first_status["domid"], changed
            assert changed["declaration_changed"] and changed["declared_spec"] != initial_spec, changed
            assert changed["retained_spec"] == initial_spec, changed
            dom0.succeed("systemctl is-active " + unit)
            roots = dom0.succeed("nix-store --query --roots " + shlex.quote(initial_spec))
            assert lease in roots, roots
            closure = dom0.succeed("nix-store --query --requisites " + shlex.quote(initial_spec)).splitlines()
            assert "${rootImage}" in closure, "The active lease must retain the immutable backing closure"
            receipt["active_generation_switch"] = {"status": changed, "gc_roots": roots, "backing_retained": True}
            receipt["first_stop"] = stop_and_check(first)
            dom0.fail(command("start"))
            assert inventory() == [] and state()["diskIdentity"] == first_state["diskIdentity"]

            dom0.succeed("${system}/bin/switch-to-configuration switch", timeout=180)
            assert not status()["declaration_changed"]
            dom0.succeed("systemctl start " + unit, timeout=150)
            second = ready({first["guest_boot_id"]})
            owned()
            assert second["marker"] == first["marker"] and second["boot_count"] == 2, second
            assert not second["marker_created"], second
            assert state()["diskIdentity"] == first_state["diskIdentity"]
            receipt["second_boot"] = second
            receipt["second_stop"] = stop_and_check(second)
            dom0.succeed(command("reset"), timeout=30)
            dom0.succeed("test ! -e " + state_dir + " && test ! -L " + lease)
            assert status()["retained_spec"] is None and inventory() == []
            receipt["reset_removed_state_and_lease"] = True

            dom0.succeed("systemctl start " + unit, timeout=150)
            third = ready({first["guest_boot_id"], second["guest_boot_id"]})
            owned()
            assert third["marker"] != first["marker"] and third["boot_count"] == 1, third
            assert third["marker_created"], third
            receipt["boot_after_reset"] = third
            receipt["third_stop"] = stop_and_check(third)
            dom0.succeed(command("reset"), timeout=30)
            dom0.succeed("test ! -e " + state_dir + " && test ! -L " + lease)
            assert inventory() == []
        ${lib.concatMapStringsSep "\n" (line: "    ${line}") (lib.splitString "\n" control.testScript)}
            receipt["status"] = "passed"
        except BaseException as error:
            receipt["status"] = "failed"
            receipt["error"] = {"type": type(error).__name__, "message": str(error)[:4096]}
            raise
        finally:
            if started:
                diagnostics: dict[str, object] = {}
                for label, query in [
                    ("inventory", "xl list --long"),
                    ("lifecycle", command("status")),
                    ("journal", "journalctl -u " + unit + " --no-pager -n 60"),
                    ("console", "tail -c 32768 " + console_dir + "/*.log 2>/dev/null"),
                ]:
                    try:
                        code, text = dom0.execute(query, timeout=15)
                        diagnostics[label] = {"exit_code": code, "output": text[-32768:]}
                    except BaseException as diagnostic_error:
                        diagnostics[label] = {"error": str(diagnostic_error)[:2048]}
                receipt["diagnostics"] = diagnostics
                try:
                    # Never substitute xl destroy for a failed graceful lifecycle operation.
                    dom0.succeed("systemctl stop " + unit, timeout=120)
                    assert inventory() == []
                    receipt["graceful_cleanup"] = True
                except BaseException as cleanup_error:
                    receipt["status"] = "failed"
                    receipt["graceful_cleanup"] = False
                    receipt["cleanup_error"] = str(cleanup_error)[:4096]
                try:
                    dom0.shutdown()
                    receipt["dom0_shutdown"] = True
                except BaseException as shutdown_error:
                    receipt["status"] = "failed"
                    receipt["dom0_shutdown"] = False
                    receipt["shutdown_error"] = str(shutdown_error)[:4096]
            (Path(os.environ["out"]) / "xen-managed-lifecycle-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        assert receipt["status"] == "passed", receipt
      '';
  };
in
{
  inherit guest rootImage test;
  plan = {
    nixpkgsRevision = nixpkgs.rev;
    guestSystem = guest.config.system.build.toplevel.outPath;
    guestKernel = guest.config.system.build.kernel.outPath;
    backingFile = backing;
    outerMemoryMiB = 6144;
    outerVcpus = 4;
    outerDiskMiB = 12288;
    guestMemoryMiB = 2048;
    guestVcpus = 2;
    imageEmbeddedInDom0 = true;
    nativeStatus = "not_run";
    gcQualification = "Registered active-spec lease and referenced backing across a live generation switch; no sole-root GC-survival claim.";
  };
}
