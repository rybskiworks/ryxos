{
  pkgs,
  baseModule,
  guest,
  rootImage,
  guestType,
  nested,
  nixpkgsRevision,
}:
let
  inherit (pkgs) lib;
  withGuest = guestType != null;
  name =
    if withGuest then
      "xen-${guestType}-${if nested then "nested-kvm" else "boot"}"
    else
      "xen-dom0-boot";
  domainName = "xen-kvm-probe";
  disk = "/dev/disk/by-id/virtio-ryxos-probe";
  kernel = "${guest.config.system.build.kernel}/${guest.config.system.boot.loader.kernelFile}";
  initrd = "${guest.config.system.build.initialRamdisk}/${guest.config.system.boot.loader.initrdFile}";
  mkGuestConfig =
    guestType: nested:
    pkgs.writeText "xen-${if nested then "kvm" else "boot"}-probe-${guestType}.cfg" ''
      name = "${domainName}"
      type = "${guestType}"
      memory = 2048
      maxmem = 2048
      vcpus = 2
      hap = 1
      nestedhvm = ${if nested then "1" else "0"}
      altp2m = 0
      kernel = "${kernel}"
      ramdisk = "${initrd}"
      # The guest disk owns its installed profile. Referring to its toplevel here
      # would also embed that complete closure in dom0's disk image.
      extra = "init=/nix/var/nix/profiles/system/init root=/dev/xvda console=hvc0 ryxos.probe=${
        if nested then "nested" else "boot"
      }"
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
      # All guest drivers share one dom0 image and select an explicit xl config.
      "xen/boot-probe-hvm.cfg".source = mkGuestConfig "hvm" false;
      "xen/kvm-probe-hvm.cfg".source = mkGuestConfig "hvm" true;
      "xen/kvm-probe-pvh.cfg".source = mkGuestConfig "pvh" true;
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
  testScript = { nodes, ... }: ''
    import json
    import os
    from pathlib import Path
    ${lib.optionalString withGuest "import uuid"}
    ${lib.optionalString withGuest "import shlex"}

    qemu_script = Path("${nodes.dom0.system.build.vm}/bin/run-${nodes.dom0.networking.hostName}-vm").read_text()
    assert "-virtfs" not in qemu_script and "-fsdev" not in qemu_script, "Host directory sharing is forbidden"
    dom0.start()
    dom0.wait_for_unit("multi-user.target")
    dom0.wait_for_unit("xenstored.service")
    dom0.succeed("grep -Fx control_d /proc/xen/capabilities")
    xl_info = dom0.succeed("xl info")
    domain_rows = dom0.succeed("xl list").strip().splitlines()[1:]
    assert len(domain_rows) == 1, domain_rows
    assert domain_rows[0].split()[1] == "0", domain_rows
    receipt: dict[str, object] = {
        "schema": 1,
        "xen_version": "${pkgs.xen.version}",
        "packages": {
            "nixpkgs_revision": "${nixpkgsRevision}",
            "outer_qemu_version": "${nodes.dom0.virtualisation.qemu.package.version}",
            "device_model_qemu_version": "${nodes.dom0.virtualisation.xen.qemu.package.version}",
            "dom0_kernel_version": "${nodes.dom0.system.build.kernel.version}",
        },
        "xl_info": xl_info[-8192:],
        "dom0_booted": True,
        "guest_type": ${if withGuest then ''"${guestType}"'' else "None"},
        "guest_kvm": None,
        "guest_boot": None,
        "nested_requested": ${if withGuest && nested then "True" else "False"},
        "host_directory_sharing": False,
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
          dom0.succeed("xl create /etc/xen/${
            if nested then "kvm" else "boot"
          }-probe-${guestType}.cfg", timeout=90)
          # The result is read from an unmounted disk only after xl reports
          # the guest absent, including the guest's own failed-probe receipt.
          dom0.wait_until_fails("xl domid ${domainName}", timeout=180)
          remaining = dom0.succeed("xl list").strip().splitlines()[1:]
          assert len(remaining) == 1 and remaining[0].split()[1] == "0", remaining
          result_text = dom0.succeed("debugfs -R 'cat /var/lib/xen-kvm-probe/result.json' ${disk} 2>/dev/null")
          result = json.loads(result_text)
          receipt["${if nested then "guest_kvm" else "guest_boot"}"] = result
          assert result.get("status") == "passed", result
          assert result.get("mode") == "${if nested then "nested" else "boot"}", result
          if ${if nested then "True" else "False"}:
              assert result.get("kvm_api") == 12, result
              assert result.get("empty_vm_created_and_closed") is True, result
          else:
              assert result.get("guest_booted") is True, result
              assert str(uuid.UUID(result["guest_boot_id"])) == result["guest_boot_id"], result
              assert result.get("command_output") == "RYXOS_XEN_BOOT_OK", result
              assert result.get("command_exit_code") == 0, result
              assert result.get("nested_probe") == "not_requested", result
              assert "kvm_api" not in result and "empty_vm_created_and_closed" not in result, result
          assert result.get("vm_booted") is False, result
          receipt["status"] = "passed"
      except BaseException as error:
          receipt["status"] = "failed"
          receipt["error"] = {"type": type(error).__name__, "message": str(error)[:4096]}
          diagnostics: dict[str, object] = {}
          try:
              code, output = dom0.execute("xl info", timeout=15)
              diagnostics["xl_info"] = {"exit_code": code, "output": output[-8192:]}
          except BaseException as diagnostic_error:
              diagnostics["xl_info_error"] = str(diagnostic_error)[:2048]
          log_script = (
              "import glob,json,os,stat\n"
              "records = []\n"
              "for path in sorted(glob.glob('/var/log/xen/qemu-dm-*'))[-4:]:\n"
              "    record = {'file': os.path.basename(path)}\n"
              "    try:\n"
              "        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)\n"
              "        with os.fdopen(fd, 'rb') as stream:\n"
              "            info = os.fstat(stream.fileno())\n"
              "            if not stat.S_ISREG(info.st_mode):\n"
              "                continue\n"
              "            stream.seek(max(0, info.st_size - 8192))\n"
              "            record['tail'] = stream.read(8192).decode('utf-8', 'replace')\n"
              "    except OSError as error:\n"
              "        record['error'] = str(error)[:512]\n"
              "    records.append(record)\n"
              "print(json.dumps(records))\n"
          )
          try:
              code, output = dom0.execute(
                  "/run/current-system/sw/bin/python3 -c " + shlex.quote(log_script), timeout=15)
              diagnostics["qemu_logs_exit_code"] = code
              diagnostics["qemu_logs"] = json.loads(output)
          except BaseException as diagnostic_error:
              diagnostics["qemu_logs_error"] = str(diagnostic_error)[:2048]
          receipt["failure_diagnostics"] = diagnostics
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
