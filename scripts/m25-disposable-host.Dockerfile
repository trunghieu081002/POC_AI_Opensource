# A systemd-enabled Ubuntu container used ONLY as the M2.5 acceptance
# suite's disposable host (scripts/m25-acceptance-ci.sh). Not a product
# artifact - dpagent itself targets real Linux servers (docs/deploy.md's
# own FAQ: "a container without systemd will not work... a systemd-enabled
# container works"), this is that second case, built reproducibly from
# this file rather than assumed to already exist on whatever machine runs
# the suite.
#
# Run privileged, with /sys/fs/cgroup bind-mounted and /run, /run/lock as
# tmpfs - see scripts/m25-acceptance-ci.sh for the exact `docker run`. A
# plain `docker run` of this image without that is exactly the FAQ's "does
# not work" case: /sbin/init needs real cgroup access to manage services.
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus ca-certificates sudo \
    && rm -rf /var/lib/apt/lists/*
CMD ["/sbin/init"]
