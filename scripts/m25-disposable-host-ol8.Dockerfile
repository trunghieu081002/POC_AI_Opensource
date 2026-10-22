# An Oracle Linux 8.10 userland with systemd, the EL8 counterpart of m25-disposable-host.Dockerfile. It is a
# CONTAINER: it shares the Docker host's kernel, SELinux mode and has no firewalld, so it proves the EL8 userland
# (dnf, the 3.6 system python, the PGDG module shadowing, rpm packaging) but NOT a native EL8 install. Run
# privileged with /sys/fs/cgroup mounted, as scripts/quickstart-acceptance-ci.sh does.
FROM oraclelinux:8
RUN dnf -y install systemd dbus sudo ca-certificates openssl curl tar gzip procps-ng util-linux \
    && dnf clean all \
    && systemctl mask getty@tty1.service systemd-logind.service console-getty.service
CMD ["/sbin/init"]
