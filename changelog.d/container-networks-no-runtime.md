### Fixed

- The container-network collector now reads a host with no docker or podman
  (a Proxmox hypervisor) as partial coverage instead of an unavailable
  collector, so such a host no longer marks a network scan incomplete and
  blocks every governed apply.
