{ pkgs }:
let
  # The upstream builder retains its populated staging tree during conversion.
  # Keep this test-only override tied to the reviewed source and exact phases;
  # a pin or image change must re-establish the staging directory's last use.
  inherit (pkgs) lib;
  sourceMatches =
    builtins.hashFile "sha256" (pkgs.path + "/nixos/lib/make-disk-image.nix")
    == "6272872ca91b571fabf43bc17e37c92710da6e95373070ea2b9e8c2dacc7a487"
    &&
      builtins.hashFile "sha256" (pkgs.path + "/pkgs/build-support/vm/default.nix")
      == "97a1264424ec7d7cdf9c4dad62f4da12bebd8f07e727c53e3df0172bb7b17808";
  releaseStaging = builtins.readFile ./staging-release.sh;
  prepare =
    drv:
    if drv.name != "nixos-disk-image" then
      drv
    else
      assert lib.assertMsg sourceMatches "Re-review staging lifetime for changed image-builder sources.";
      assert lib.assertMsg (
        builtins.hashString "sha256" (drv.preVM or "")
        == "d78267baa8518b4bd391530c96309dc9a7e01b3dc7c00cc4e6985f42013b1e05"
        &&
          builtins.hashString "sha256" (drv.postVM or "")
          == "8139751e9d48503641bd4f51e605dcf354c1a0bd11f8e63c5825c4bfb5ce770c"
        &&
          builtins.hashString "sha256" (drv.buildCommand or "")
          == "687b46d941094fbf59f9bb2bdd4344391cc1ec6074563fd86511a091e3d66d93"
      ) "Re-review the exact lifecycle image phases before releasing staging.";
      drv.overrideAttrs (old: {
        preVM = old.preVM + releaseStaging;
      });
in
pkgs
// {
  vmTools = pkgs.vmTools // {
    runInLinuxVM = drv: pkgs.vmTools.runInLinuxVM (prepare drv);
  };
}
