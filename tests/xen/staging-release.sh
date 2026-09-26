# This is evaluated by the builder after successful image population.
if [[ "$root" != "$PWD/root" || "$diskImage" != nixos.raw ]]; then
  echo 'Unexpected image-builder staging paths; refusing cleanup.' >&2
  exit 1
fi
if [[ ! -d "$root" || -L "$root" || ! -f "$diskImage" || -L "$diskImage" ]]; then
  echo 'Expected a private staging directory and regular raw image.' >&2
  exit 1
fi
if [[ "$(realpath -e -- "$root")" != "$(pwd -P)/root" ]]; then
  echo 'Staging directory escaped the private build directory.' >&2
  exit 1
fi
# Store copies contain read-only directories. Do not follow their symlinks.
find -P "$root" -xdev -type d -exec chmod u+w -- {} +
rm -rf --one-file-system -- "$root"
test ! -e "$root" && test ! -L "$root"
