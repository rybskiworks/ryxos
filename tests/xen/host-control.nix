{ pkgs, keys }:
let
  inherit (pkgs) lib;
in
{
  module = {
    virtualisation.qemu.networkingOptions = lib.mkOverride 0 [
      "-nic none"
      "-netdev user,id=ssh0,restrict=on,ipv6=off,hostfwd=tcp:127.0.0.1:22222-10.0.2.15:22222"
      "-device virtio-net-pci,netdev=ssh0,mac=52:54:00:72:78:01"
    ];
    virtualisation.restrictNetwork = true;
    networking = {
      useDHCP = false;
      usePredictableInterfaceNames = false;
      enableIPv6 = false;
      interfaces.eth0.ipv4.addresses = [
        {
          address = "10.0.2.15";
          prefixLength = 24;
        }
      ];
      defaultGateway = "10.0.2.2";
      firewall.enable = true;
      firewall.interfaces.eth0.allowedTCPPorts = [ 22222 ];
    };
    systemd.services.sshd = {
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
    };
  };
  initialState = "host_transport: Any = None\n";
  beforeStart = ''
    assert launcher.count("-netdev ") == 1 and launcher.count("hostfwd=") == 1
    assert "-netdev user,id=ssh0,restrict=on,ipv6=off,hostfwd=tcp:127.0.0.1:22222-10.0.2.15:22222" in launcher
    assert "-device virtio-net-pci,netdev=ssh0,mac=52:54:00:72:78:01" in launcher
    import runpy
    transport = runpy.run_path(${builtins.toJSON "${./host-transport.py}"})["HostTransport"]
    host_transport = transport(${builtins.toJSON "${pkgs.openssh}/bin/ssh"},
        ${builtins.toJSON (toString keys.snakeOilEd25519PrivateKey)},
        ${builtins.toJSON (toString keys.snakeOilPrivateKey)},
        ${builtins.toJSON keys.snakeOilPublicKey}, Path(os.environ["out"]))
    host_transport.preflight()
  '';
  clientScript = ''
    def ssh_call(remote=None, options=None, timeout=20, key=None, log_level="ERROR"):
        return host_transport.call(remote, options, timeout, key, log_level)

  '';
  beforeActions = ''
    ssh_receipt["host_listener"] = host_transport.verify_listener(dom0.pid)
    ssh_receipt["expected_audit_peer"] = "10.0.2.2"
    ssh_receipt["host_egress_probe"] = host_transport.prove_host_egress_denied(dom0)
  '';
  beforeReceipt = ''
    ssh_receipt["host_network_connection"] = "host_listener" in ssh_receipt and bool(ssh_receipt["actions"])
  '';
  afterShutdown = ''
    if host_transport is not None:
        host_transport.finish_receipt(receipt)
        if "ssh_control" in receipt:
            (Path(os.environ["out"]) / "xen-control-receipt.json").write_text(json.dumps(receipt["ssh_control"], indent=2) + "\n")
  '';
}
