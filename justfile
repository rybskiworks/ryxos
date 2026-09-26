set shell := ["bash", "-euo", "pipefail", "-c"]

fmt:
    nix fmt

evaluate:
    nix flake check --no-build --no-update-lock-file

check:
    nix flake check --no-update-lock-file -L

preview:
    nix run .#preview

desktop:
    nix run .#desktop-preview
