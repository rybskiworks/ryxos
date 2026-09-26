{ lib }:
text:
let
  booleanKeys = lib.concatStringsSep "|" [
    "merge-activate"
    "conflict-rate-limit-is-aggregate"
    "perms-activate"
    "perms-watch-activate"
    "quota-activate"
  ];
  correctLine =
    line:
    let
      boolean = builtins.match "([ \t]*)(${booleanKeys})([ \t]*=[ \t]*)(1?)([ \t]*)" line;
      accessLog = builtins.match "([ \t]*)acesss-log-nb-chars([ \t]*=.*)" line;
    in
    if boolean != null then
      "${builtins.elemAt boolean 0}${builtins.elemAt boolean 1}${builtins.elemAt boolean 2}"
      + (if builtins.elemAt boolean 3 == "1" then "true" else "false")
      + builtins.elemAt boolean 4
    else if accessLog != null then
      "${builtins.elemAt accessLog 0}access-log-nb-chars${builtins.elemAt accessLog 1}"
    else
      line;
in
lib.concatStringsSep "\n" (map correctLine (lib.splitString "\n" text))
