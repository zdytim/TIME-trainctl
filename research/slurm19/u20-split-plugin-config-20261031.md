# Isolated controller and stock-node plugin separation

On 2026-10-31 a disposable, networkless u20 container used the staged
`7a06cff` controller and its plugin directory, while stock u20 `slurmd` and
stock `libslurmfull.so` used a separate copy of the host's Slurm plugin
directory. The fixture generated two private configuration files which differed
in `PluginDir`; it removed `SlurmdParameters=config_overrides`. The node's
library search was restricted to copied stock libraries and the container's
system directories. `/proc` mappings at node registration showed at least one
plugin from the stock directory and no mapping from the candidate directory.

Using synthetic GPU resources, an exhausted UID 1001 job remained PENDING
with `monthly_capacity_exhausted` and `NodeList=(null)`. The broker's grant
and retry completed; the job subsequently ran as UID 1001 and finished.
The passing image `time-stock-node:328bec3` had SHA-256
`3af9ef9def89ab0334b2ffb9f3a875e3f4fb815acbe18b89ec6da0bcb4c2fb5e`.
The fixture script was committed in `328bec3`, SHA-256
`7fbf4aee0df0268870f05363475dc97c42146d7aa75e905a7b996c643e2dac8d`.
The copied host MUNGE and Lua runtime libraries had SHA-256 values
`d89adc8a3e916baa5cf63ce3fd14f468effb283e7a2bb078218296a9d8d8ab44` and
`9e254c93064b940a87b1917ec97f89fb8e5941bdff509d3ed14b9410b5af1b38`,
respectively.

An initial run failed because the container hostname lacked a mapping. A later
run completed the workload but its node library search still included the
candidate path; the mapping assertion detected a candidate-path MUNGE library,
so that run does not establish isolated node dependencies. The passing run
removed that search path and repeated the assertion. The assertion checks node
process mappings at registration, not every plugin loaded by later job steps.

This is evidence for one possible split configuration, not a production
cutover: the container used `task/none`, fake GPU resources, private state and
a synthetic hostname, and did not run the real systemd unit, production PAM,
Cockpit or real GPU. It does not prove all stock-node and candidate-controller
ABIs, plugin loading throughout job execution, configuration distribution,
reboot handling, or rollback safety. No host service or configuration changed.
After the run the host queue was empty, all three platform/Slurm services were
active, `/dev/nvidia0` retained `other::---`, and `SlurmctldParameters` was
unset. Monthly enforcement remains disabled.
