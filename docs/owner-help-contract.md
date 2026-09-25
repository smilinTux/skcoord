# Owner-requested assistance (d3be5b44)

An active source owner may delegate an explicit, isolated helper without changing
the parent's claim, acceptance criteria or dependencies. The helper carries its
exact input repository/ref/revision, parent claim generation, requested output,
allowed paths and verification commands. There is no minimum task duration.

`Board.create_helper_task` uses the existing local authority lock order:
board, parent card, explicit child creation. It revalidates the folded owner and
claim inside those locks. The existing explicit creation journal provides replay
and interrupted-write recovery; the new operation never writes raw event files.
This is a local authority operation, not a distributed lock across Syncthing peers.
Call it through the supported coordination authority, never concurrently on stale
replicas. A helper may produce isolated source/evidence only; it cannot publish,
deploy, change shared state or complete the parent. Normal helper admission still
enforces inherited restrictions and dependencies.

The contract digest also binds the parent's title, description and criteria, so a
new restriction written before the creation lock invalidates a prepared request.
Sibling write-path overlap is checked during creation, including nested paths;
read-only requests can coexist. This API does not sandbox a worker's filesystem,
fence later publication, or prove a retired remote worker is stopped. Those are
execution-controller responsibilities, and no such new runtime behavior is
claimed by this source candidate. The owner must checkpoint and stop editing the
delegated paths before a helper starts; integration remains with that owner.

Verification covers stale/foreign claims, changed owner while waiting for a lock,
claim conflicts, frozen/terminal parent, malformed and conflicting bindings,
dependency cycles, request replay/conflicts, overlapping active helper paths,
read-only assistance, interrupted legacy projection recovery and unchanged parent.
Runtime installation, cluster resumption and provider changes are outside this
source candidate.
