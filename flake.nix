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
      xenLifecycle = import ./tests/xen/lifecycle.nix {
        inherit nixpkgs pkgs baseModule;
        xenDomainsModule = self.nixosModules.xenDomains;
        xenControlModule = self.nixosModules.xenControl;
      };
      xenHostControl = import ./tests/xen/lifecycle.nix {
        inherit nixpkgs pkgs baseModule;
        xenDomainsModule = self.nixosModules.xenDomains;
        xenControlModule = self.nixosModules.xenControl;
        sshTransport = "host-loopback";
      };
      labRunner = pkgs.writeShellApplication {
        name = "ryxos-lab-run";
        runtimeInputs = [
          pkgs.python3
          pkgs.systemd
          pkgs.util-linux
          pkgs.coreutils
          pkgs.lix
        ];
        text = ''
          exec python3 ${./scripts}/lab-supervisor.py "$@"
        '';
      };
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
        xenControl = import ./modules/xen-control.nix;
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
        lab-runner = labRunner;
        xen-hvm-smoke = pkgs.writeShellApplication {
          name = "xen-hvm-smoke";
          text = ''
            if [[ $# != 1 ]]; then
              echo "usage: xen-hvm-smoke EVIDENCE_DIRECTORY" >&2
              exit 2
            fi
            exec ${labRunner}/bin/ryxos-lab-run \
              --driver ${xenLab.tests.hvm.driver}/bin/nixos-test-driver \
              --output-dir "$1" --memory-mib 6144 --qemu-overhead-mib 2048 --vcpus 4
          '';
        };
        xen-lifecycle-smoke = pkgs.writeShellApplication {
          name = "xen-lifecycle-smoke";
          text = ''
            if [[ $# != 1 ]]; then
              echo "usage: xen-lifecycle-smoke EVIDENCE_DIRECTORY" >&2
              exit 2
            fi
            exec ${labRunner}/bin/ryxos-lab-run \
              --driver ${xenLifecycle.test.driver}/bin/nixos-test-driver \
              --output-dir "$1" --memory-mib 6144 --qemu-overhead-mib 2048 --vcpus 4 \
              --timeout-seconds 1500
          '';
        };
        xen-control-host-smoke = pkgs.writeShellApplication {
          name = "xen-control-host-smoke";
          text = ''
            if [[ $# != 1 ]]; then
              echo "usage: xen-control-host-smoke EVIDENCE_DIRECTORY" >&2
              exit 2
            fi
            exec ${labRunner}/bin/ryxos-lab-run \
              --driver ${xenHostControl.test.driver}/bin/nixos-test-driver \
              --output-dir "$1" --memory-mib 6144 --qemu-overhead-mib 2048 --vcpus 4 \
              --timeout-seconds 1500 --network-profile loopback-ssh
          '';
        };
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
        xen-lifecycle-test-driver = xenLifecycle.test.driver;
        xen-control-host-test-driver = xenHostControl.test.driver;
        xen-experimental-hvm-nested-test-driver = xenLab.tests.experimentalHvmNested.driver;
        xen-experimental-pvh-nested-test-driver = xenLab.tests.experimentalPvhNested.driver;
        xen-experimental-hvm-nested-test = xenLab.tests.experimentalHvmNested;
        xen-experimental-pvh-nested-test = xenLab.tests.experimentalPvhNested;
        default = self.packages.${system}.preview;
      };
      apps.${system} = {
        xen-lab-preflight = {
          type = "app";
          program = "${self.packages.${system}.xen-lab-preflight}/bin/xen-lab-preflight";
        };
        lab-runner = {
          type = "app";
          program = "${labRunner}/bin/ryxos-lab-run";
        };
        xen-hvm-smoke = {
          type = "app";
          program = "${self.packages.${system}.xen-hvm-smoke}/bin/xen-hvm-smoke";
        };
        xen-lifecycle-smoke = {
          type = "app";
          program = "${self.packages.${system}.xen-lifecycle-smoke}/bin/xen-lifecycle-smoke";
        };
        xen-control-host-smoke = {
          type = "app";
          program = "${self.packages.${system}.xen-control-host-smoke}/bin/xen-control-host-smoke";
        };
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
          python3 -m unittest discover -s ${./tests/xen} -p 'test_*.py'
          touch "$out"
        '';
        xen-domains = import ./tests/xen-domains.nix { inherit pkgs; };
        xen-control = import ./tests/xen-control.nix { inherit pkgs; };
        xenstore-config = import ./tests/xenstore-config.nix { inherit pkgs; };
        xen-dom0 = xenLab.tests.dom0;
        xen-hvm = xenLab.tests.hvm;
        xen-lifecycle = xenLifecycle.test;
        xen-control-host = xenHostControl.test;
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
