{
  description = "Reusable NixOS profiles for Lix, desktops and virtual machines";
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/a799d3e3886da994fa307f817a6bc705ae538eeb";
    tooling = {
      url = "github:rybskiworks/nix-tooling/ec0d6c798461907dec299f30d9bc009adba0b9c1";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };
  outputs =
    {
      self,
      nixpkgs,
      tooling,
    }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      baseModule = {
        imports = [
          tooling.nixosModules.lixSystem
          ./modules/base.nix
        ];
      };
      mkSystem = import ./lib/mk-system.nix { inherit baseModule; };
      xenLab = import ./tests/xen { inherit nixpkgs pkgs baseModule; };
      example =
        modules:
        mkSystem {
          inherit nixpkgs pkgs modules;
          stateVersion = "26.05";
        };
    in
    {
      nixosModules = {
        base = baseModule;
        desktop = import ./modules/desktop.nix;
        guest = import ./profiles/guest.nix;
        virtualization = import ./modules/virtualization.nix;
        virtualizingGuest = import ./profiles/virtualizing-guest.nix;
        nonvirtualizingGuest = import ./profiles/nonvirtualizing-guest.nix;
        xenHost = import ./profiles/xen-host.nix;
        xenGuest = import ./profiles/xen-guest.nix;
        xenDomains = import ./modules/xen-domains.nix;
      };
      lib = { inherit mkSystem; };
      nixosConfigurations = {
        preview = example [ ./examples/preview.nix ];
        desktop-preview = example [
          ./examples/preview.nix
          ./modules/desktop.nix
          ({ pkgs, ... }: {
            ryxos.desktop.enable = true;
            virtualisation.graphics = true;
            virtualisation.qemu.package = pkgs.qemu;
            virtualisation.qemu.options = [
              "-vga none"
              "-device virtio-vga-gl"
              "-display gtk,gl=on"
            ];
          })
        ];
        leaf-preview = example [
          ./examples/preview.nix
          ./profiles/nonvirtualizing-guest.nix
        ];
      };
      packages.${system} = {
        xen-lab-preflight = pkgs.writeShellApplication {
          name = "xen-lab-preflight";
          runtimeInputs = [
            pkgs.python3
            pkgs.util-linux
          ];
          text = ''
            exec python3 ${./scripts/lab-preflight.py} "$@"
          '';
        };
        preview = self.nixosConfigurations.preview.config.system.build.vm;
        desktop-preview = self.nixosConfigurations.desktop-preview.config.system.build.vm;
        leaf-preview = self.nixosConfigurations.leaf-preview.config.system.build.vm;
        xen-dom0-test-driver = xenLab.tests.dom0.driver;
        xen-hvm-test-driver = xenLab.tests.hvm.driver;
        xen-experimental-hvm-nested-test-driver = xenLab.tests.experimentalHvmNested.driver;
        xen-experimental-pvh-nested-test-driver = xenLab.tests.experimentalPvhNested.driver;
        xen-experimental-hvm-nested-test = xenLab.tests.experimentalHvmNested;
        xen-experimental-pvh-nested-test = xenLab.tests.experimentalPvhNested;
        default = self.packages.${system}.preview;
      };
      apps.${system}.xen-lab-preflight = {
        type = "app";
        program = "${self.packages.${system}.xen-lab-preflight}/bin/xen-lab-preflight";
      };
      checks.${system} = {
        lab-preflight =
          pkgs.runCommand "ryxos-lab-preflight-tests" { nativeBuildInputs = [ pkgs.python3 ]; }
            ''
              export PYTHONDONTWRITEBYTECODE=1
              python3 -m unittest discover -s ${./.}/tests -p 'test_*.py'
              touch "$out"
            '';
        xen-probes = pkgs.runCommand "ryxos-xen-probe-tests" { nativeBuildInputs = [ pkgs.python3 ]; } ''
          export PYTHONDONTWRITEBYTECODE=1
          python3 -m unittest discover -s ${./tests/xen} -p 'test_probe.py'
          touch "$out"
        '';
        xen-domains = import ./tests/xen-domains.nix { inherit pkgs; };
        xen-dom0 = xenLab.tests.dom0;
        xen-hvm = xenLab.tests.hvm;
        leaf = import ./tests/leaf.nix {
          inherit pkgs baseModule;
          leafModule = self.nixosModules.nonvirtualizingGuest;
        };
        preview = self.nixosConfigurations.preview.config.system.build.toplevel;
        desktop = self.nixosConfigurations.desktop-preview.config.system.build.toplevel;
      };
      formatter.${system} = pkgs.nixfmt-tree;
    };
}
