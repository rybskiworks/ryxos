{
  pkgs,
  baseModule,
  leafModule,
}:
pkgs.testers.runNixOSTest {
  name = "ryxos-nonvirtualizing-guest";
  nodes.machine = {
    imports = [
      baseModule
      leafModule
    ];
    system.stateVersion = "26.05";
    virtualisation.memorySize = 1536;
  };
  testScript = ''
    start_all()
    machine.wait_for_unit("multi-user.target")
    machine.succeed("nix --version | grep Lix")
    machine.succeed("systemctl is-active nix-daemon.socket ryxos-no-kvm.service")
    machine.fail("command -v workestrate")
    machine.fail("command -v msb")
    machine.fail("test -e /dev/kvm")
    machine.fail("modprobe kvm_amd")
    machine.fail("modprobe kvm_intel")
    machine.shutdown()
  '';
}
