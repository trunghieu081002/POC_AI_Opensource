# Superset runtime pack acceptance

Runtime implementation `289dcbb` passed authenticated query, API and database
write denial, actual dbt table recreation with reader SELECT preserved, reinstall
without duplicate admin/connection, service restart and scoped rollback. Runtime
rollback retained warehouse and encrypted metadata. Recovery added at `eb8e3b1`
then restored from the same external secrets and passed query, denied writes,
restart and exact retained metadata identity. Its only later source change at
`4e754bf` is the unit dry-run fixture parameter map.

The runtime suite passed **4/4**, lifecycle and recovery exited 0. Failed attempts
(API engine discovery dependency and shared Rich output parsing) are retained.
Cachetools is explicitly pinned and engine discovery is checked before liveness;
console logs are rendered literally by the separate renderer-fix PR.

Shared-source regression at 4e754bf: full unit **1326/26**, usage **3/3**, bronze
**34/34**, both leak audits 0. See `../final-source-regression/README.md` including
the legacy-cgroup systemd restart failure/recovery limitation.

Final audit removed the lifecycle connection/table, the separate native-BI probe
table, confirmed zero fixture connections, retained protected BI reports and
removed the verified owned Superset container. Both owned test containers are
removed; existing company-container IDs remain 11/11. No decommission of company
metadata, warehouse, services or containers occurred.

Scope: Ubuntu 22.04 disposable stack on Oracle Linux 8.10 Docker host. Oracle
Linux native runtime installation, browser dashboards, HG composed pipeline/BI
and representative load are not established by this runtime PR.
