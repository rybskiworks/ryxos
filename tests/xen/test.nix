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
  guestKernelParams =
    nested:
    [ "init=/nix/var/nix/profiles/system/init" ]
    ++ guest.config.boot.kernelParams
    ++ [ "ryxos.probe=${if nested then "nested" else "boot"}" ];
  mkGuestConfig =
    guestType: nested:
    pkgs.writeText "xen-${if nested then "kvm" else "boot"}-probe-${guestType}.cfg" ''
      name = "${domainName}"
      uuid = "d2f61444-6530-4d88-bd40-56b99561a4da"
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
      extra = ${builtins.toJSON (lib.concatStringsSep " " (guestKernelParams nested))}
      ${lib.optionalString (guestType == "hvm") ''
        # New disposable HVM guests use the machine ABI provided by QEMU 11.
        device_model_args_hvm = [ "-machine", "xenfv-4.2,suppress-vmdesc=on" ]
      ''}
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
    import runpy
    import shlex
    from pathlib import Path
    ${lib.optionalString withGuest "import uuid"}

    qemu_script = Path("${nodes.dom0.system.build.vm}/bin/run-${nodes.dom0.networking.hostName}-vm").read_text()
    assert "-virtfs" not in qemu_script and "-fsdev" not in qemu_script, "Host directory sharing is forbidden"
    assert "-machine q35" in qemu_script and "-no-user-config" in qemu_script, "Explicit outer QEMU policy is missing"
    dom0.start()
    dom0.wait_for_unit("multi-user.target")
    dom0.wait_for_unit("xenstored.service")
    xenstore = runpy.run_path("${./xenstore.py}")["check"](
        dom0, "${nodes.dom0.virtualisation.xen.store.path}")
    dom0.succeed("grep -Fx control_d /proc/xen/capabilities")
    xl_info = dom0.succeed("xl info")
    domain_rows = dom0.succeed("xl list").strip().splitlines()[1:]
    assert len(domain_rows) == 1, domain_rows
    assert domain_rows[0].split()[1] == "0", domain_rows
    boot_entry_script = (
        "import json,pathlib\n"
        "config = pathlib.Path('${nodes.dom0.boot.loader.efi.efiSysMountPoint}/loader/loader.conf').read_text()\n"
        "defaults = [line.split()[1] for line in config.splitlines() if line.split() and line.split()[0] == 'default']\n"
        "assert len(defaults) == 1 and defaults[0].startswith('xen-'), defaults\n"
        "entry = pathlib.Path('/sys/firmware/efi/efivars/LoaderEntrySelected-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f')\n"
        "selected = None\n"
        "if entry.exists():\n"
        "    raw = entry.read_bytes()\n"
        "    assert 6 <= len(raw) <= 4096, 'Invalid EFI entry size'\n"
        "    selected = raw[4:].decode('utf-16-le').rstrip('\\0')\n"
        "    assert selected.startswith('xen-') and pathlib.PurePosixPath(selected).name == selected and '\\0' not in selected, selected\n"
        "print(json.dumps({'configured_default': defaults[0], 'observed_selected': selected, 'selection_evidence': 'uefi-variable' if selected else 'configured-default-only'}))\n"
    )
    boot_entry = json.loads(dom0.succeed(
        "/run/current-system/sw/bin/python3 -c " + shlex.quote(boot_entry_script)))
    bootctl_code, bootctl_output = dom0.execute("bootctl status --no-pager", timeout=15)
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
        "xenstore": xenstore,
        "boot_entry": boot_entry,
        "bootctl_status": {"exit_code": bootctl_code, "output": bootctl_output[-8192:]},
        "outer_machine": "q35",
        "qemu_user_config": False,
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
          create_command = "xl create /etc/xen/${if nested then "kvm" else "boot"}-probe-${guestType}.cfg"
          # The initrd owns root-mount policy through its generated parameters.
          # A handwritten root device duplicates systemd's fstab mount unit.
          receipt["guest_kernel_cmdline"] = ${builtins.toJSON (lib.concatStringsSep " " (guestKernelParams nested))}
          receipt["guest_uuid"] = "d2f61444-6530-4d88-bd40-56b99561a4da"
          if ${if guestType == "hvm" then "True" else "False"}:
              receipt["device_model_machine"] = "xenfv-4.2"
          dom0.succeed(create_command, timeout=90)
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
          for diagnostic_name, command in [
              ("guest_state", "xl list -l ${domainName}"),
              ("guest_console", "set -o pipefail; timeout --kill-after=2s 8s xl console -t pv -n 0 ${domainName} </dev/null 2>&1 | tail -c 32768"),
          ]:
              try:
                  code, output = dom0.execute(command, timeout=15)
                  diagnostics[diagnostic_name] = {
                      "exit_code": code,
                      "output": output[-32768:],
                      # A running guest keeps its console open. This timeout
                      # bounds read-only capture and is not a new boot failure.
                      "capture_timed_out": diagnostic_name == "guest_console" and code == 124,
                  }
              except BaseException as diagnostic_error:
                  diagnostics[diagnostic_name + "_error"] = str(diagnostic_error)[:2048]
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
