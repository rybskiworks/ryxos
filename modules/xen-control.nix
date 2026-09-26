{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.ryxos.xenControl;
  account = "ryxos-xen-control";
  actions = [
    "status"
    "start"
    "stop"
    "reset"
  ];
  # Keep dependency assertions evaluable when the required module is disabled.
  helper =
    if config.ryxos.xenDomains.enable then
      "${config.system.build.ryxosXenDomainHelper}/bin/ryxos-xen-domain"
    else
      "/missing-required-xen-domain-helper";
  policy = pkgs.writeText "xen-control-policy.json" (
    builtins.toJSON {
      schema = 1;
      inherit helper;
      domains = cfg.domains;
    }
  );
  # This is also the login shell. SSH's -c argument is deliberately ignored;
  # only SSH_ORIGINAL_COMMAND passes through the exact parser.
  dispatcher = pkgs.writeTextFile {
    name = "ryxos-xen-control";
    destination = "/bin/ryxos-xen-control";
    executable = true;
    text = ''
      #!${pkgs.python3}/bin/python3 -I
      import runpy
      import sys
      sys.argv = [${builtins.toJSON "${./xen-control.py}"}, ${builtins.toJSON "${policy}"}]
      runpy.run_path(sys.argv[0], run_name="__main__")
    '';
  };
  command = "${dispatcher}/bin/ryxos-xen-control";
  publicKey =
    key:
    builtins.match "(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com) [A-Za-z0-9+/]+={0,2}( [ -~]*)?" key
    != null;
  permittedEnvironment = [
    "LANG"
    "LC_*"
  ];
in
{
  options.ryxos.xenControl = {
    enable = lib.mkEnableOption "restricted public-key SSH control of selected Xen domains";
    domains = lib.mkOption {
      type = lib.types.listOf (lib.types.strMatching "[a-zA-Z][a-zA-Z0-9_-]{0,62}");
      default = [ ];
      description = "Explicit nonempty subset of the currently declared Xen domain names available to this operator account.";
    };
    authorizedKeys = lib.mkOption {
      type = lib.types.listOf lib.types.singleLineStr;
      default = [ ];
      description = ''
        Public SSH keys enrolled for the dedicated operator account. Supply
        ordinary key lines without authorized_keys options or certificates.
        No keys, host keys, network listeners or firewall rules are generated.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = config.services.openssh.enable;
        message = "ryxos.xenControl requires an explicitly enabled OpenSSH service.";
      }
      {
        assertion = config.ryxos.xenDomains.enable;
        message = "ryxos.xenControl requires ryxos.xenDomains.enable.";
      }
      {
        assertion = config.security.sudo.enable && config.services.openssh.settings.UsePAM == true;
        message = "ryxos.xenControl requires sudo and OpenSSH PAM account handling for its password-locked key-only account.";
      }
      {
        assertion =
          cfg.domains != [ ]
          && builtins.length cfg.domains <= 256
          && builtins.length cfg.domains == builtins.length (lib.unique cfg.domains)
          && lib.all (name: builtins.hasAttr name config.ryxos.xenDomains.domains) cfg.domains;
        message = "ryxos.xenControl.domains must uniquely select 1 to 256 currently declared Xen domains.";
      }
      {
        assertion = cfg.authorizedKeys != [ ] && lib.all publicKey cfg.authorizedKeys;
        message = "ryxos.xenControl.authorizedKeys requires ordinary public key lines, without options or certificates.";
      }
      {
        assertion = (config.services.openssh.settings.PermitUserEnvironment or false) == false;
        message = "ryxos.xenControl requires PermitUserEnvironment=false; account-owned environment files are not permitted.";
      }
      {
        assertion =
          config.services.openssh.settings.AcceptEnv == null
          || lib.all (
            name: builtins.elem name permittedEnvironment
          ) config.services.openssh.settings.AcceptEnv;
        message = "ryxos.xenControl requires AcceptEnv to be unset or restricted to LANG and LC_*; arbitrary startup environment admission is unsafe.";
      }
      {
        assertion =
          config.users.users.${account}.extraGroups == [ ]
          && config.users.users.${account}.openssh.authorizedKeys.keyFiles == [ ];
        message = "ryxos.xenControl's dedicated account must not acquire supplementary groups or independently managed key files.";
      }
    ];
    users.groups.${account} = { };
    users.users.${account} = {
      isSystemUser = true;
      group = account;
      home = "/var/empty";
      createHome = false;
      hashedPassword = "!";
      shell = command;
      openssh.authorizedKeys.keys = lib.mkForce (map (key: "restrict ${key}") cfg.authorizedKeys);
    };
    security.sudo.extraRules = [
      {
        users = [ account ];
        runAs = "root";
        commands = lib.concatMap (
          name:
          map (action: {
            command = "${helper} ${action} ${name}";
            options = [
              "NOPASSWD"
              "NOSETENV"
            ];
          }) actions
        ) cfg.domains;
      }
    ];
    system.build.ryxosXenControlDispatcher = dispatcher;
    # Precede caller Match blocks; global settings remain outside this block.
    services.openssh.extraConfig = lib.mkOrder 1 ''
      Match User ${account}
        AuthenticationMethods publickey
        PubkeyAuthentication yes
        PasswordAuthentication no
        KbdInteractiveAuthentication no
        HostbasedAuthentication no
        AuthorizedKeysFile /etc/ssh/authorized_keys.d/${account}
        AuthorizedKeysCommand none
        AuthorizedPrincipalsFile none
        AuthorizedPrincipalsCommand none
        TrustedUserCAKeys none
        ForceCommand ${command}
        DisableForwarding yes
        AllowTcpForwarding no
        AllowStreamLocalForwarding no
        AllowAgentForwarding no
        X11Forwarding no
        PermitTTY no
        PermitTunnel no
        PermitUserRC no
        PermitOpen none
        PermitListen none
        GatewayPorts no
        MaxSessions 1
        AcceptEnv LANG LC_*
        SetEnv LANG=C LC_ALL=C
      Match All
    '';
  };
}
