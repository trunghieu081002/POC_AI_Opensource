#!/usr/bin/env bash
# Shared helpers for this pack's steps. Sourced after dp.sh.

sl_data_dir()  { dp_param data_dir /var/lib/silo/data; }
sl_env_file()  { echo /etc/default/silo; }
sl_bin()       { echo /usr/bin/silo; }

# Confirmed against both real, downloaded packages (2026-09-28, .rpm and
# .deb - identical layout in each): ships /usr/bin/silo,
# /usr/lib/systemd/system/silo.service (its own unit, this pack does not
# write one), /usr/lib/sysusers.d/silo.conf (creates the 'silo' user itself
# - this pack does not run useradd), and reads /etc/default/silo
# (EnvironmentFile=-, same MINIO_* interface MinIO's own unit used). All
# four (family x arch) download URLs this pack can construct were also
# fetched for real and returned real package bytes, not just a 200 on HEAD.
DEFAULT_RELEASE="RELEASE.2026-09-16T00-00-00Z"

sl_arch() {
  case "$(uname -m)" in
    x86_64) echo "amd64" ;;
    aarch64|arm64) echo "arm64" ;;
    *) echo "" ;;
  esac
}

# rpm's arch names its files by uname -m (x86_64/aarch64), deb by dpkg's
# amd64/arm64 - both differ from the "amd64"/"arm64" this pack otherwise
# uses (matched to systemd/OS-family conventions elsewhere in this library).
sl_rpm_arch() {
  case "$1" in
    amd64) echo "x86_64" ;;
    arm64) echo "aarch64" ;;
  esac
}

sl_package_url() {
  local arch="$1" release="$2"
  local ver="${release#RELEASE.}"
  ver="$(echo "$ver" | tr -dc '0-9')"   # RELEASE.2026-09-16T00-00-00Z -> 20260916000000
  if [ "$arch" = "amd64" ]; then
    if dp_is_debian; then
      echo "https://github.com/pgsty/silo/releases/download/${release}/silo_${ver}.0.0-1PGSTY_amd64.deb"
    else
      echo "https://github.com/pgsty/silo/releases/download/${release}/silo-${ver}.0.0-1PGSTY.x86_64.rpm"
    fi
  else
    if dp_is_debian; then
      echo "https://github.com/pgsty/silo/releases/download/${release}/silo_${ver}.0.0-1PGSTY_arm64.deb"
    else
      echo "https://github.com/pgsty/silo/releases/download/${release}/silo-${ver}.0.0-1PGSTY.aarch64.rpm"
    fi
  fi
}
