"""Probe the KVM API instead of inferring capability from a device node."""

import argparse
import fcntl
import json
import os
import sys


def probe():
    try:
        device = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
    except OSError as error:
        return {"usable": False, "stage": "open", "errno": error.errno}
    try:
        version = fcntl.ioctl(device, 0xAE00, 0)
        if version != 12:
            return {"usable": False, "stage": "api", "version": version}
        vm = fcntl.ioctl(device, 0xAE01, 0)
        os.close(vm)
        return {"usable": True, "api_version": version}
    except OSError as error:
        return {"usable": False, "stage": "create_vm", "errno": error.errno}
    finally:
        os.close(device)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--require", action="store_true")
    mode.add_argument("--forbid", action="store_true")
    args = parser.parse_args()
    result = probe()
    print(json.dumps(result, sort_keys=True))
    sys.exit(0 if result["usable"] == args.require else 1)
