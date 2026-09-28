#!/usr/bin/env bash
# Shared helpers for this pack's steps. Sourced after dp.sh.

mn_install_dir() { dp_param install_dir /opt/minio; }
mn_data_dir()    { dp_param data_dir /opt/minio/data; }
mn_bin()         { echo "$(mn_install_dir)/bin/minio"; }
mn_env_file()    { echo "$(mn_install_dir)/minio.env"; }

# MinIO ships one static binary per (OS, arch) - no per-distro package, which
# is why this pack has no repo step the way postgres does. `uname -m` is the
# only branch that matters; OS family is irrelevant to which binary to fetch.
mn_arch() {
  case "$(uname -m)" in
    x86_64) echo "amd64" ;;
    aarch64|arm64) echo "arm64" ;;
    *) echo "" ;;
  esac
}

# Empty release (the default) tracks MinIO's own non-versioned "current
# stable" URL - what MinIO's own systemd install guide fetches. A pinned
# RELEASE.<timestamp> tag (see pack.yaml's own comment) uses the archive path
# instead, for a reproducible install.
mn_download_url() {
  local arch="$1" release="$2"
  if [ -n "$release" ]; then
    echo "https://dl.min.io/server/minio/release/linux-${arch}/archive/minio.${release}"
  else
    echo "https://dl.min.io/server/minio/release/linux-${arch}/minio"
  fi
}
