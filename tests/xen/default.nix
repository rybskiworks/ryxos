{
  nixpkgs,
  pkgs,
  baseModule,
}:
let
  inherit (pkgs) lib;
  guest = nixpkgs.lib.nixosSystem {
    system = pkgs.stdenv.hostPlatform.system;
    modules = [
      baseModule
      ./domu.nix
      { nixpkgs.pkgs = pkgs; }
    ];
  };
  rootImage = import (nixpkgs + "/nixos/lib/make-disk-image.nix") {
    inherit pkgs lib;
    inherit (guest) config;
    name = "xen-kvm-probe-root";
    baseName = "xen-kvm-probe";
    # The image is attached at dom0 boot; keep its label distinct from dom0's.
    label = "ryxos-probe";
    format = "raw";
    partitionTableType = "none";
    installBootLoader = false;
    copyChannel = false;
    diskSize = "auto";
    additionalSpace = "128M";
  };
  mkTest =
    guestType:
    import ./test.nix {
      inherit
        pkgs
        guest
        rootImage
        guestType
        baseModule
        ;
    };
in
{
  inherit rootImage;
  tests = {
    dom0 = mkTest null;
    hvm = mkTest "hvm";
    pvh = mkTest "pvh";
  };
  plan = {
    nixpkgsRevision = nixpkgs.rev;
    xenVersion = pkgs.xen.version;
    outerMemoryMiB = 6144;
    outerVcpus = 4;
    dom0MemoryMiB = 2048;
    guestMemoryMiB = 2048;
    guestVcpus = 2;
    guestSystem = guest.config.system.build.toplevel.outPath;
    guestKernel = "${guest.config.system.build.kernel}/${guest.config.system.boot.loader.kernelFile}";
    guestInitrd = "${guest.config.system.build.initialRamdisk}/${guest.config.system.boot.loader.initrdFile}";
    rootImage = "${rootImage}/xen-kvm-probe.img";
    guestDisk = {
      dom0Device = "/dev/disk/by-id/virtio-ryxos-probe";
      qemuSnapshot = true;
      embeddedInDom0 = false;
    };
    testStatus = "not_run";
    scope = "Xen-in-KVM boot and domU KVM API; no nested workload boot or physical-host qualification";
  };
}
