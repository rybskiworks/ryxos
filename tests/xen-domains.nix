{ pkgs }:
let
  inherit (pkgs) lib;
  sample = {
    uuid = "f3f70b01-3a87-4b5a-a77e-6238f6b78123";
    configFile = pkgs.writeText "sample-xl.cfg" ''
      name = "sample"
      uuid = "f3f70b01-3a87-4b5a-a77e-6238f6b78123"
    '';
  };
  evaluate =
    extra:
    (import (pkgs.path + "/nixos/lib/eval-config.nix") {
      system = pkgs.stdenv.hostPlatform.system;
      modules = [
        ../modules/xen-domains.nix
        {
          nixpkgs.pkgs = pkgs;
          system.stateVersion = "26.05";
          boot.loader.systemd-boot.enable = true;
          boot.initrd.systemd.enable = true;
          virtualisation.xen.enable = true;
          systemd.services.xendomains.enable = false;
          ryxos.xenDomains = {
            enable = true;
            domains.sample = sample;
          };
        }
        extra
      ];
    }).config;
  valid = evaluate { };
  disabled = evaluate { ryxos.xenDomains.enable = lib.mkForce false; };
  automatic = evaluate { ryxos.xenDomains.domains.sample.autoStart = true; };
  twoAutomatic = evaluate {
    ryxos.xenDomains.domains = {
      sample.autoStart = true;
      second = sample // {
        uuid = "e3f70b01-3a87-4b5a-a77e-6238f6b78123";
        autoStart = true;
      };
    };
  };
  duplicate = evaluate { ryxos.xenDomains.domains.second = sample; };
  twoManual = evaluate {
    ryxos.xenDomains.domains.second = sample // {
      uuid = "e3f70b01-3a87-4b5a-a77e-6238f6b78123";
    };
  };
  threeManual = evaluate {
    ryxos.xenDomains.domains = {
      second = sample // {
        uuid = "e3f70b01-3a87-4b5a-a77e-6238f6b78123";
      };
      third = sample // {
        uuid = "d3f70b01-3a87-4b5a-a77e-6238f6b78123";
      };
    };
  };
  xenDisabled = evaluate { virtualisation.xen.enable = lib.mkForce false; };
  legacyEnabled = evaluate { systemd.services.xendomains.enable = lib.mkForce true; };
  assertionsFor =
    configuration:
    builtins.filter (
      item:
      !item.assertion
      && (
        lib.hasPrefix "ryxos.xenDomains" item.message
        || lib.hasPrefix "Disable systemd.services.xendomains" item.message
        || lib.hasPrefix "Xen domain names" item.message
        || lib.hasPrefix "Managed Xen domains" item.message
        || lib.hasPrefix "Every managed Xen domain" item.message
      )
    ) configuration.assertions;
  admitted = configuration: assertionsFor configuration == [ ];
  contract = [
    (admitted valid)
    (!(admitted duplicate))
    (!(admitted xenDisabled))
    (!(admitted legacyEnabled))
    (!(disabled.systemd.services ? ryxos-xen-sample))
    (valid.systemd.services.ryxos-xen-sample.wantedBy == [ ])
    (automatic.systemd.services.ryxos-xen-sample.wantedBy == [ "multi-user.target" ])
    (builtins.elem "ryxos-xen-sample.service" twoAutomatic.systemd.services.ryxos-xen-second.after)
    (builtins.elem "ryxos-xen-sample.service" twoManual.systemd.services.ryxos-xen-second.after)
    (twoManual.systemd.services.ryxos-xen-second.wantedBy == [ ])
    (!(builtins.elem "ryxos-xen-sample.service" twoManual.systemd.services.ryxos-xen-second.requires))
    # Starting/stopping sample and third remains ordered even with no job for second.
    (builtins.elem "ryxos-xen-sample.service" threeManual.systemd.services.ryxos-xen-third.after)
    (builtins.elem "ryxos-xen-second.service" threeManual.systemd.services.ryxos-xen-third.after)
    (valid.systemd.services.ryxos-xen-sample.serviceConfig.KillMode == "process")
    (!valid.ryxos.xenDomains.domains.sample.destroyOnTimeout)
  ];
in
assert lib.assertMsg (builtins.all (value: value) contract) "Xen lifecycle module contract failed";
pkgs.runCommand "xen-domain-lifecycle-tests"
  {
    nativeBuildInputs = [ pkgs.python3 ];
  }
  ''
    mkdir -p source/modules source/tests
    cp ${../modules/xen-domain.py} source/modules/xen-domain.py
    cp ${./test_xen_domain.py} source/tests/test_xen_domain.py
    python3 -B -m unittest discover -s source/tests -p test_xen_domain.py -v
    touch "$out"
  ''
