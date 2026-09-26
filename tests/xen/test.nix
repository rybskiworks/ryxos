{
  pkgs,
  baseModule,
  guest,
  rootImage,
  guestType,
}:
let
  inherit (pkgs) lib;
  withGuest = guestType != null;
  name = if withGuest then "xen-${guestType}-kvm" else "xen-dom0-boot";
  domainName = "xen-kvm-probe";
  disk = "/dev/disk/by-id/virtio-ryxos-probe";
  kernel = "${guest.config.system.build.kernel}/${guest.config.system.boot.loader.kernelFile}";
  initrd = "${guest.config.system.build.initialRamdisk}/${guest.config.system.boot.loader.initrdFile}";
  mkGuestConfig =
    guestType:
    pkgs.writeText "xen-kvm-probe-${guestType}.cfg" ''
      name = "${domainName}"
      type = "${guestType}"
      memory = 2048
      maxmem = 2048
      vcpus = 2
      hap = 1
      nestedhvm = 1
      altp2m = 0
      kernel = "${kernel}"
      ramdisk = "${initrd}"
      extra = "init=${guest.config.system.build.toplevel}/init root=/dev/xvda console=hvc0"
      disk = [ "format=raw,vdev=xvda,access=rw,backendtype=phy,target=${disk}" ]
      vif = [ ]
      on_poweroff = "destroy"
      on_reboot = "destroy"
      on_crash = "destroy"
    '';
in
pkgs.testers.runNixOSTest {
  inherit name;
  globalTimeout = 600;
  qemu.forceAccel = true;
  nodes.dom0 = { ... }: {
    imports = [
      baseModule
      ./dom0.nix
    ];
    environment.etc = lib.optionalAttrs withGuest {
      # Both drivers share one dom0 image; only the selected xl config differs.
      "xen/kvm-probe-hvm.cfg".source = mkGuestConfig "hvm";
      "xen/kvm-probe-pvh.cfg".source = mkGuestConfig "pvh";
    };
    # The immutable image stays on the outer host. QEMU writes only to a
    # temporary overlay; dom0 passes that synthetic block device to Xen.
    virtualisation.qemu.drives = lib.mkAfter (
      lib.optionals withGuest [
        {
          name = "ryxos-probe";
          file = "${rootImage}/xen-kvm-probe.img";
          driveExtraOpts = {
            format = "raw";
            snapshot = "on";
            werror = "report";
          };
          deviceExtraOpts.serial = "ryxos-probe";
        }
      ]
    );
  };
  testScript = ''
    import json
    import os
    from pathlib import Path

    dom0.start()
    dom0.wait_for_unit("multi-user.target")
    dom0.wait_for_unit("xenstored.service")
    dom0.succeed("grep -Fx control_d /proc/xen/capabilities")
    dom0.succeed("xl info")
    domain_rows = dom0.succeed("xl list").strip().splitlines()[1:]
    assert len(domain_rows) == 1, domain_rows
    assert domain_rows[0].split()[1] == "0", domain_rows
    receipt = {
        "schema": 1,
        "xen_version": "${pkgs.xen.version}",
        "dom0_booted": True,
        "guest_type": ${if withGuest then ''"${guestType}"'' else "None"},
        "guest_kvm": None,
        "physical_host_qualified": False,
        "nested_workload_booted": False,
    }
    ${lib.optionalString withGuest ''
      receipt["status"] = "running"
      try:
          dom0.succeed("udevadm settle && test -b ${disk}")
          dom0.fail("findmnt --source ${disk}")
          dom0.fail("test -e ${rootImage}")
          receipt["guest_disk"] = {
              "dom0_device": "${disk}",
              "qemu_snapshot_configured": True,
              "image_absent_from_dom0_store": True,
          }
          dom0.succeed("xl create /etc/xen/kvm-probe-${guestType}.cfg", timeout=90)
          # The result is read from an unmounted disk only after xl reports
          # the guest absent, including the guest's own failed-probe receipt.
          dom0.wait_until_fails("xl domid ${domainName}", timeout=180)
          remaining = dom0.succeed("xl list").strip().splitlines()[1:]
          assert len(remaining) == 1 and remaining[0].split()[1] == "0", remaining
          result_text = dom0.succeed("debugfs -R 'cat /var/lib/xen-kvm-probe/result.json' ${disk} 2>/dev/null")
          result = json.loads(result_text)
          receipt["guest_kvm"] = result
          assert result.get("status") == "passed", result
          assert result.get("kvm_api") == 12, result
          assert result.get("empty_vm_created_and_closed") is True, result
          assert result.get("vm_booted") is False, result
          receipt["status"] = "passed"
      except BaseException as error:
          receipt["status"] = "failed"
          receipt["error"] = {"type": type(error).__name__, "message": str(error)[:4096]}
          raise
      finally:
          # This VM is disposable and this fixed name was absent before create.
          # Never run this test script in a physical or shared dom0.
          try:
              dom0.execute("xl destroy ${domainName}")
              dom0.fail("xl domid ${domainName}")
              receipt["guest_cleanup"] = "passed"
          except BaseException as cleanup_error:
              receipt["guest_cleanup"] = "failed"
              receipt["cleanup_error"] = {
                  "type": type(cleanup_error).__name__,
                  "message": str(cleanup_error)[:4096],
              }
              if receipt["status"] != "failed":
                  receipt["status"] = "failed"
                  raise
          finally:
              (Path(os.environ["out"]) / "${name}-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    ''}
    (Path(os.environ["out"]) / "${name}-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    dom0.shutdown()
  '';
}
