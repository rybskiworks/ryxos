{
  pkgs,
  domainName,
  xenControlModule,
  transport ? "guest-loopback",
}:
let
  inherit (pkgs) lib;
  # These keys are public test fixtures from nixpkgs, never deployment keys.
  keys = import (pkgs.path + "/nixos/tests/ssh-keys.nix") pkgs;
  account = "ryxos-xen-control";
  directory = "/etc/ryxos-control-test";
  port = 22222;
  hostMode = transport == "host-loopback";
  host = import ./host-control.nix { inherit pkgs keys; };
  listener = if hostMode then "10.0.2.15" else "127.0.0.1";
  auditPeer = if hostMode then "10.0.2.2" else "127.0.0.1";
  indent =
    text:
    lib.concatMapStrings (line: if line == "" then "" else "    ${line}\n") (lib.splitString "\n" text);
  clientScript =
    if hostMode then
      host.clientScript
    else
      ''
        ssh_target = "${account}@127.0.0.1"
        ssh_base = [
            "${pkgs.openssh}/bin/ssh", "-F", "/dev/null", "-p", "${toString port}",
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=${directory}/known_hosts",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5",
            "-o", "ConnectionAttempts=1",
        ]

        def ssh_call(remote=None, options=None, timeout=20, key="${directory}/operator_ed25519", log_level="ERROR"):
            arguments = ssh_base + ["-i", key, "-o", "LogLevel=" + log_level]
            arguments += (["-T"] if options is None else options) + [ssh_target]
            if remote is not None:
                arguments.append(remote)
            # Both timeouts are bounded. All interpolation is an argv quoted by
            # shlex; adversarial remote text never executes in the client's shell.
            arguments = ["timeout", "--signal=TERM", "--kill-after=2s", str(timeout) + "s"] + arguments
            return dom0.execute(shlex.join(arguments) + " </dev/null 2>&1", timeout=timeout + 5)

      '';
in
assert lib.elem transport [
  "guest-loopback"
  "host-loopback"
];
{
  module = {
    imports = [ xenControlModule ] ++ lib.optional hostMode host.module;
    services.openssh = {
      enable = true;
      openFirewall = false;
      ports = lib.mkForce [ port ];
      listenAddresses = lib.mkForce [
        {
          addr = listener;
          inherit port;
        }
      ];
      hostKeys = lib.mkForce [
        {
          type = "rsa";
          path = "${directory}/host_rsa";
        }
      ];
      settings = {
        UsePAM = true;
        PermitRootLogin = "no";
        PasswordAuthentication = false;
        KbdInteractiveAuthentication = false;
        LogLevel = "VERBOSE";
      };
    };
    ryxos.xenControl = {
      enable = true;
      domains = [ domainName ];
      authorizedKeys = [ keys.snakeOilEd25519PublicKey ];
    };
    environment.etc = {
      "ryxos-control-test/host_rsa" = {
        source = keys.snakeOilPrivateKey;
        mode = "0600";
      };
      "ryxos-control-test/operator_ed25519" = {
        source = keys.snakeOilEd25519PrivateKey;
        mode = "0600";
      };
      "ryxos-control-test/known_hosts".text = "[127.0.0.1]:${toString port} ${keys.snakeOilPublicKey}\n";
    };
  };

  initialState = lib.optionalString hostMode host.initialState;
  beforeStart = lib.optionalString hostMode host.beforeStart;
  afterShutdown = lib.optionalString hostMode host.afterShutdown;

  # Compose inside the lifecycle test's try block, after its final absent reset.
  # That test supplies dom0, receipt, inventory(), owned(), ready(previous_ids),
  # and the first_boot/second_boot/boot_after_reset guest readiness receipts.
  testScript = ''
    ssh_receipt: dict[str, Any] = {
        "schema": 1, "status": "running", "transport": "${transport}",
        "production_credentials": False, "synthetic_keys": True,
        "host_network_connection": False, "operator_host_route_qualified": False,
        "actions": {}, "refusals": {},
    }
    receipt["ssh_control"] = ssh_receipt
    ssh_guest_may_exist = False
  ''
  + clientScript
  + ''
    def ssh_action(action, timeout=20):
        code, output = ssh_call(action + " ${domainName}", timeout=timeout)
        assert code == 0, (action, code, output[-4096:])
        value = json.loads(output)
        assert value["name"] == "${domainName}", value
        ssh_receipt["actions"][action] = value
        return value

    def ssh_refused(label, remote, options=None):
        dom0.succeed("journalctl --sync", timeout=15)
        previous = json.loads(dom0.succeed(
            "journalctl -t ryxos-xen-control -n 1 -o json --no-pager"))
        cursor = previous["__CURSOR"]
        code, output = ssh_call(remote, options=options)
        assert code == 64, (label, code, output[-4096:])
        dom0.succeed("journalctl --sync", timeout=15)
        records = dom0.succeed("journalctl -t ryxos-xen-control --after-cursor "
                               + shlex.quote(cursor) + " -o json --no-pager")
        events = [json.loads(json.loads(line)["MESSAGE"]) for line in records.splitlines()]
        assert events == [{"account": "${account}", "peer": "${auditPeer}",
                           "result": "refused", "exit_code": 64}], (label, events)
        diagnostic = "Only status, start, stop or reset" in output
        # Subsystem channels may suppress extended stderr; the exact exit code
        # and one new server-side refusal must still agree for that request.
        assert diagnostic or label == "subsystem", (label, code, output[-4096:])
        ssh_receipt["refusals"][label] = {
            "exit_code": code, "parser_diagnostic": diagnostic, "audit_refusal": True,
        }

    try:
        assert inventory() == [], "SSH qualification must start with no managed guest"
        dom0.wait_for_unit("sshd.service")
        listeners = dom0.succeed("ss -H -ltn 'sport = :${toString port}'").strip().splitlines()
        assert len(listeners) == 1 and listeners[0].split()[3] == "${listener}:${toString port}", listeners
        ${
          if hostMode then
            ''assert sorted(dom0.succeed("ls -1 /sys/class/net").split()) == ["eth0", "lo"]''
          else
            ''assert dom0.succeed("ls -1 /sys/class/net").strip() == "lo"''
        }
        ssh_receipt["listener"] = "${listener}:${toString port}"
    ${lib.optionalString hostMode (indent host.beforeActions)}    initial = ssh_action("status")
        assert initial["status"] == "absent" and initial["retained_spec"] is None, initial

        for label, remote in [
            ("unknown_name", "start undeclared"),
            ("extra_argument", "status ${domainName} extra"),
            ("traversal", "status ../${domainName}"),
            ("shell_metacharacter", "status ${domainName}; touch /tmp/ssh-control-escape"),
            ("newline", "status ${domainName}\nstart ${domainName}"),
            ("force_stop", "force-stop ${domainName}"),
            ("list", "list"),
            ("shell", None),
        ]:
            ssh_refused(label, remote)
        ssh_refused("subsystem", "sftp", options=["-T", "-s"])
        dom0.succeed("test ! -e /tmp/ssh-control-escape")
        assert inventory() == [], "Refused commands must not create guests"

        code, output = ssh_call("status ${domainName}", key=${
          if hostMode then "host_transport.wrong_key" else builtins.toJSON "${directory}/host_rsa"
        })
        assert code == 255 and "Permission denied (publickey)" in output, (code, output[-4096:])
        ssh_receipt["refusals"]["unenrolled_key"] = {"exit_code": code, "authentication_diagnostic": True}

        code, output = ssh_call("status ${domainName}", options=["-tt"])
        assert "PTY allocation request failed" in output, (code, output[-4096:])
        # OpenSSH may continue the admitted command after refusing its PTY.
        assert code in (0, 255) and "Connection refused" not in output, (code, output[-4096:])
        ssh_receipt["refusals"]["pty"] = {"exit_code": code, "allocation_denied": True}

        code, output = ssh_call(options=["-T", "-W", "127.0.0.1:${toString port}"], log_level="DEBUG1")
        assert code == 255 and "administratively prohibited" in output, (code, output[-4096:])
        ssh_receipt["refusals"]["direct_forward"] = {"exit_code": code, "administrative_diagnostic": True}
        code, output = ssh_call(options=["-T", "-N", "-o", "ExitOnForwardFailure=yes",
                                       "-R", "127.0.0.1:22223:127.0.0.1:${toString port}"], log_level="DEBUG1")
        assert code == 255 and "remote port forwarding failed" in output, (code, output[-4096:])
        assert not dom0.succeed("ss -H -ltn 'sport = :22223'").strip()
        ssh_receipt["refusals"]["remote_forward"] = {"exit_code": code, "forwarding_diagnostic": True}

        # The account cannot skip the dispatcher by invoking unrelated sudo.
        code, output = dom0.execute("runuser -u ${account} -- sudo -n /run/current-system/sw/bin/id </dev/null 2>&1", timeout=15)
        assert code != 0 and ("not allowed" in output or "a password is required" in output), (code, output[-4096:])
        ssh_receipt["refusals"]["unrelated_sudo"] = {"exit_code": code, "sudo_diagnostic": True}

        ssh_guest_may_exist = True
        ssh_action("start", timeout=180)
        prior_boots = {receipt[key]["guest_boot_id"] for key in ("first_boot", "second_boot", "boot_after_reset")}
        observed_boot = ready(prior_boots)
        observed = ssh_action("status")
        assert observed["owned"] and observed["status"] == "present", observed
        assert observed["domid"] == owned()["domid"]
        assert observed_boot["boot_count"] == 1 and observed_boot["marker_created"], observed_boot
        ssh_receipt["guest_boot"] = observed_boot
        code, output = ssh_call("reset ${domainName}")
        assert code == 1 and "Reset requires confirmed absence" in output, (code, output[-4096:])
        assert owned()["domid"] == observed["domid"]
        ssh_receipt["refusals"]["live_reset"] = {"exit_code": code, "lifecycle_diagnostic": True}
        ssh_action("stop", timeout=120)
        assert inventory() == []
        absent = ssh_action("status")
        assert absent["status"] == "absent" and absent["receipt_phase"] == "stopped", absent
        ssh_action("reset", timeout=30)
        assert ssh_action("status")["retained_spec"] is None and inventory() == []
        ssh_guest_may_exist = False
        dom0.succeed("test ! -e /var/lib/ryxos-xen-domains/${domainName} && test ! -L /nix/var/nix/gcroots/ryxos-xen-domains/${domainName}")
        ssh_receipt["status"] = "passed"
    except BaseException as ssh_error:
        ssh_receipt["status"] = "failed"
        ssh_receipt["error"] = {"type": type(ssh_error).__name__, "message": str(ssh_error)[-4096:]}
        raise
    finally:
        if ssh_guest_may_exist:
            try:
                code, output = dom0.execute("/run/current-system/sw/bin/ryxos-xen-domain stop ${domainName}", timeout=120)
                ssh_receipt["failure_cleanup"] = {"exit_code": code, "output": output[-4096:], "guest_absent": inventory() == []}
                if code != 0 or not ssh_receipt["failure_cleanup"]["guest_absent"]:
                    ssh_receipt["status"] = "failed"
            except BaseException as ssh_cleanup_error:
                ssh_receipt["status"] = "failed"
                ssh_receipt["cleanup_error"] = str(ssh_cleanup_error)[-4096:]
        ssh_receipt["diagnostics"] = {}
        for label, query in [
            ("sshd", "journalctl -u sshd.service --no-pager -n 100"),
            ("dispatcher", "journalctl -t ryxos-xen-control --no-pager -n 100"),
            ("inventory", "xl list --long"),
        ]:
            try:
                code, output = dom0.execute(query, timeout=15)
                ssh_receipt["diagnostics"][label] = {"exit_code": code, "output": output[-32768:]}
            except BaseException as ssh_log_error:
                ssh_receipt["diagnostics"][label] = {"error": str(ssh_log_error)[-2048:]}
        try:
            dom0.succeed("systemctl stop sshd.service", timeout=20)
            assert not dom0.succeed("ss -H -ltn 'sport = :${toString port}'").strip()
            ssh_receipt["listener_stopped"] = True
        except BaseException as ssh_stop_error:
            ssh_receipt["status"] = "failed"
            ssh_receipt["listener_stopped"] = False
            ssh_receipt["listener_stop_error"] = str(ssh_stop_error)[-2048:]
    ${lib.optionalString hostMode (indent host.beforeReceipt)}    (Path(os.environ["out"]) / "xen-control-receipt.json").write_text(json.dumps(ssh_receipt, indent=2) + "\n")
    assert ssh_receipt["status"] == "passed", ssh_receipt
  '';
}
