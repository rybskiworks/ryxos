{ pkgs }:
let
  inherit (pkgs) lib;
  correct = import ../lib/xenstore-config.nix { inherit lib; };
  evaluate =
    corrected: extra:
    (import (pkgs.path + "/nixos/lib/eval-config.nix") {
      system = pkgs.stdenv.hostPlatform.system;
      modules = [
        {
          nixpkgs.pkgs = pkgs;
          system.stateVersion = "26.05";
          boot.initrd.systemd.enable = true;
          boot.loader.systemd-boot.enable = true;
          virtualisation.xen.enable = true;
        }
        extra
      ]
      ++ lib.optional corrected ../modules/xenstore-config.nix;
    }).config;
  settings = value: {
    virtualisation.xen.store.settings = {
      enableMerge = value;
      conflict.rateLimitIsAggregate = value;
      perms.enable = value;
      perms.enableWatch = value;
      quota.enable = value;
      quota.maxSize = 7890;
      xenstored.accessLog.nbChars = 1379;
    };
    environment.etc."xen/oxenstored.conf".text = lib.mkAfter "# preserve caller text\n";
    environment.etc."unrelated.conf".text = "quota-activate = 1\nacesss-log-nb-chars = 37\n";
  };
  enabled = evaluate true (settings true);
  disabled = evaluate true (settings false);
  defaults = evaluate true { };
  upstream = evaluate false (settings true);
  mixed = evaluate true {
    virtualisation.xen.store.settings = {
      enableMerge = false;
      conflict.rateLimitIsAggregate = true;
      perms.enable = true;
      perms.enableWatch = false;
      quota.enable = true;
      xenstored.accessLog.nbChars = 421;
    };
  };
  unchangedText = "quota-activate = 1\nacesss-log-nb-chars = 37\n";
  noXen = evaluate true {
    virtualisation.xen.enable = lib.mkForce false;
    environment.etc."xen/oxenstored.conf".text = unchangedText;
  };
  cStore = evaluate true {
    virtualisation.xen.store.path = "${pkgs.xen}/bin/xenstored";
    environment.etc."xen/oxenstored.conf".text = unchangedText;
  };
  explicitSource = pkgs.writeText "operator-oxenstored.conf" "quota-activate = true\n";
  sourceOverride = evaluate true {
    environment.etc."xen/oxenstored.conf".source = lib.mkForce explicitSource;
  };
  textOf = cfg: cfg.environment.etc."xen/oxenstored.conf".text;
  sourceOf = cfg: cfg.environment.etc."xen/oxenstored.conf".source;
  linesOf = text: lib.splitString "\n" text;
  booleanKeys = [
    "merge-activate"
    "conflict-rate-limit-is-aggregate"
    "perms-activate"
    "perms-watch-activate"
    "quota-activate"
  ];
  hasValue =
    text: key: value:
    builtins.elem "${key} = ${value}" (linesOf text);
  hasBooleans = cfg: value: lib.all (key: hasValue (textOf cfg) key value) booleanKeys;
  unrelatedLines =
    text:
    builtins.filter (
      line:
      !(lib.any (key: lib.hasPrefix "${key} =" line) (
        booleanKeys
        ++ [
          "access-log-nb-chars"
          "acesss-log-nb-chars"
        ]
      ))
    ) (linesOf text);
  alreadyCorrect = ''
    merge-activate = false
    conflict-rate-limit-is-aggregate = true
    perms-activate = true
    perms-watch-activate = false
    quota-activate = false
    access-log-nb-chars = 421
    # quota-activate = 1
    unrelated-key = 1
  '';
  contract = {
    trueValues = hasBooleans enabled "true";
    falseValues = hasBooleans disabled "false";
    defaultPolicy = hasBooleans defaults "true";
    customLogBound = hasValue (textOf enabled) "access-log-nb-chars" "1379";
    typoRemoved = !(lib.hasInfix "acesss-log-nb-chars" (textOf enabled));
    unrelatedSettings = hasValue (textOf enabled) "quota-maxsize" "7890";
    callerText = lib.hasInfix "# preserve caller text\n" (textOf enabled);
    upstreamPreserved = unrelatedLines (textOf upstream) == unrelatedLines (textOf enabled);
    generatedSource = (sourceOf enabled).text == textOf enabled;
    falseGeneratedSource = (sourceOf disabled).text == textOf disabled;
    mixedValues =
      lib.all (key: hasValue (textOf mixed) key "true") [
        "conflict-rate-limit-is-aggregate"
        "perms-activate"
        "quota-activate"
      ]
      && lib.all (key: hasValue (textOf mixed) key "false") [
        "merge-activate"
        "perms-watch-activate"
      ];
    correctedInputUnchanged = correct alreadyCorrect == alreadyCorrect;
    invalidInputUnchanged = correct "quota-activate = invalid\n" == "quota-activate = invalid\n";
    otherFileUnchanged = enabled.environment.etc."unrelated.conf".text == unchangedText;
    disabledXenUnchanged = textOf noXen == unchangedText;
    cStoreUnchanged = textOf cStore == unchangedText;
    explicitSourceUnchanged = sourceOf sourceOverride == explicitSource;
  };
  fixtures = {
    inherit contract;
    parser = "${pkgs.xen}/bin/oxenstored";
    xenVersion = pkgs.xen.version;
    valid = {
      enabled = textOf enabled;
      disabled = textOf disabled;
      defaults = textOf defaults;
      mixed = textOf mixed;
    };
  };
  fixturesFile = pkgs.writeText "xenstore-config-fixtures.json" (builtins.toJSON fixtures);
in
assert lib.assertMsg (lib.all (value: value) (
  builtins.attrValues contract
)) "OCaml Xenstore configuration contract failed";
pkgs.runCommand "xenstore-config-tests"
  {
    passthru = { inherit fixtures fixturesFile; };
  }
  ''
    # The strict Xen parser initializes its control interface even in config-test
    # mode. Native dom0 fixtures run that matrix; this gate checks rendering.
    cp ${fixturesFile} "$out"
  ''
