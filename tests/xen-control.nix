{ pkgs }:
let
  inherit (pkgs) lib;
  account = "ryxos-xen-control";
  publicKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB synthetic";
  evaluate =
    extra:
    (import (pkgs.path + "/nixos/lib/eval-config.nix") {
      system = pkgs.stdenv.hostPlatform.system;
      modules = [
        ../modules/xen-domains.nix
        ../modules/xen-control.nix
        {
          nixpkgs.pkgs = pkgs;
          system.stateVersion = "26.05";
          boot.loader.systemd-boot.enable = true;
          boot.initrd.systemd.enable = true;
          virtualisation.xen.enable = true;
          systemd.services.xendomains.enable = false;
          services.openssh.enable = true;
          ryxos.xenDomains = {
            enable = true;
            domains.example = {
              uuid = "f3f70b01-3a87-4b5a-a77e-6238f6b78123";
              configFile = pkgs.writeText "synthetic-xl.cfg" "name = 'example'";
            };
          };
          ryxos.xenControl = {
            enable = true;
            domains = [ "example" ];
            authorizedKeys = [ publicKey ];
          };
        }
        extra
      ];
    }).config;
  failures =
    configuration:
    builtins.filter (
      item: !item.assertion && lib.hasPrefix "ryxos.xenControl" item.message
    ) configuration.assertions;
  refused = extra: failures (evaluate extra) != [ ];
  valid = evaluate { };
  disabled = evaluate { ryxos.xenControl.enable = lib.mkForce false; };
  helper = "${valid.system.build.ryxosXenDomainHelper}/bin/ryxos-xen-domain";
  rules = builtins.filter (rule: rule.users == [ account ]) valid.security.sudo.extraRules;
  commands = lib.concatMap (rule: rule.commands) rules;
  accountConfig = valid.users.users.${account};
  # Reproduce the pinned OpenSSH module's formatter and verify its exact source
  # command. The syntax fixture keeps identical bytes but drops Nix references;
  # checking text must not realize Xen, domain images or executable targets.
  settingsFile =
    (pkgs.formats.keyValue {
      mkKeyValue = lib.generators.mkKeyValueDefault {
        mkValueString =
          value:
          if lib.isInt value then
            toString value
          else if lib.isString value then
            value
          else if value == true then
            "yes"
          else if value == false then
            "no"
          else
            throw "Unsupported OpenSSH setting type";
      } " ";
    }).generate
      "sshd.conf-settings"
      (
        lib.mapAttrs (
          key: value:
          if lib.isList value then
            if
              builtins.elem key [
                "Ciphers"
                "KexAlgorithms"
                "Macs"
              ]
            then
              lib.concatStringsSep "," value
            else if
              builtins.elem key [
                "AcceptEnv"
                "AuthorizedKeysFile"
                "AllowGroups"
                "AllowUsers"
                "DenyGroups"
                "DenyUsers"
              ]
            then
              lib.concatStringsSep " " value
            else
              throw "Unsupported OpenSSH list setting"
          else
            value
        ) (lib.filterAttrs (_: value: value != null) valid.services.openssh.settings)
      );
  expectedSourceCommand = ''
    cat ${settingsFile} - >$out <<EOL
    ${valid.services.openssh.extraConfig}
    EOL
  '';
  syntaxConfig = pkgs.writeText "xen-control-syntax.conf" (
    builtins.unsafeDiscardStringContext (settingsFile.text + valid.services.openssh.extraConfig + "\n")
  );
  contract = {
    valid = failures valid == [ ];
    disabled = failures disabled == [ ] && !(disabled.users.users ? ${account});
    noSsh = refused { services.openssh.enable = lib.mkForce false; };
    noDomains = refused { ryxos.xenControl.domains = lib.mkForce [ ]; };
    unknownDomain = refused { ryxos.xenControl.domains = lib.mkForce [ "other" ]; };
    noLifecycle = refused { ryxos.xenDomains.enable = lib.mkForce false; };
    noSudo = refused { security.sudo.enable = lib.mkForce false; };
    noPam = refused { services.openssh.settings.UsePAM = lib.mkForce false; };
    noKeys = refused { ryxos.xenControl.authorizedKeys = lib.mkForce [ ]; };
    keyOptions = refused {
      ryxos.xenControl.authorizedKeys = lib.mkForce [ "command=\"id\" ${publicKey}" ];
    };
    dangerousEnvironment = refused { services.openssh.settings.AcceptEnv = [ "*" ]; };
    userEnvironment = refused { services.openssh.settings.PermitUserEnvironment = true; };
    supplementaryGroup = refused { users.users.${account}.extraGroups = [ "wheel" ]; };
    exactSudoActions =
      map (command: command.command) commands == map (action: "${helper} ${action} example") [
        "status"
        "start"
        "stop"
        "reset"
      ];
    exactSudoAuthority =
      builtins.length rules == 1
      && lib.all (rule: rule.runAs == "root") rules
      && lib.all (
        command:
        command.options == [
          "NOPASSWD"
          "NOSETENV"
        ]
      ) commands;
    restrictedAccount =
      accountConfig.isSystemUser
      && accountConfig.hashedPassword == "!"
      && accountConfig.home == "/var/empty"
      && !accountConfig.createHome
      && accountConfig.extraGroups == [ ]
      && accountConfig.openssh.authorizedKeys.keys == [ "restrict ${publicKey}" ]
      && accountConfig.shell == "${valid.system.build.ryxosXenControlDispatcher}/bin/ryxos-xen-control";
    firewallUnchanged =
      valid.networking.firewall.allowedTCPPorts == disabled.networking.firewall.allowedTCPPorts
      && valid.services.openssh.openFirewall == disabled.services.openssh.openFirewall;
    exactGeneratedSyntax =
      valid.environment.etc."ssh/sshd_config".source.buildCommand == expectedSourceCommand;
  };
  effectiveExpected = pkgs.writeText "xen-control-effective-policy.json" (
    builtins.unsafeDiscardStringContext (
      builtins.toJSON {
        authenticationmethods = "publickey";
        forcecommand = accountConfig.shell;
        passwordauthentication = "no";
        kbdinteractiveauthentication = "no";
        disableforwarding = "yes";
        allowtcpforwarding = "no";
        allowstreamlocalforwarding = "no";
        allowagentforwarding = "no";
        x11forwarding = "no";
        permittty = "no";
        permittunnel = "no";
        permituserrc = "no";
        permitopen = "none";
        permitlisten = "none";
        gatewayports = "no";
        maxsessions = "1";
        authorizedkeysfile = "/etc/ssh/authorized_keys.d/${account}";
        authorizedkeyscommand = "none";
        authorizedprincipalsfile = "none";
        authorizedprincipalscommand = "none";
        trustedusercakeys = "none";
        permituserenvironment = "no";
      }
    )
  );
in
assert lib.assertMsg (lib.all (value: value) (builtins.attrValues contract))
  "Xen SSH control module contract failed: ${
    lib.concatStringsSep ", " (builtins.attrNames (lib.filterAttrs (_: value: !value) contract))
  }";
pkgs.runCommand "xen-control-tests"
  {
    nativeBuildInputs = [ pkgs.python3 ];
  }
  ''
    mkdir -p source/modules source/tests
    cp ${../modules/xen-control.py} source/modules/xen-control.py
    cp ${./test_xen_control.py} source/tests/test_xen_control.py
    python3 -B -m unittest discover -s source/tests -p test_xen_control.py -v
    # -G skips host-key availability checks. No daemon or authentication runs.
    # Path existence, sudo execution and authentication belong to native tests.
    ${pkgs.openssh}/bin/sshd -G -t -f ${syntaxConfig}
    ${pkgs.openssh}/bin/sshd -G -T -f ${syntaxConfig} \
      -C user=${account},host=localhost,addr=192.0.2.10,lport=22 > effective-sshd.txt
    python3 - ${effectiveExpected} <<'PY'
    import json
    from pathlib import Path
    import sys
    lines = Path("effective-sshd.txt").read_text().splitlines()
    effective = dict(line.split(" ", 1) for line in lines)
    for key, value in json.loads(Path(sys.argv[1]).read_text()).items():
        assert effective[key] == value, (key, effective.get(key), value)
    accepted = [line.removeprefix("acceptenv ") for line in lines if line.startswith("acceptenv ")]
    assert accepted == ["LANG", "LC_*"], accepted
    PY
    touch "$out"
  ''
