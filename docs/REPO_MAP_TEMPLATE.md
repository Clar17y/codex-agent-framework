# Repository map and decisions

- Checkout identity: `<repository>@<worktree or checkout path>`
- Author: `<name or agent>`
- Revision: `<candidate HEAD>`
- Source paths inspected: `<paths>`
- Invalidation: `<what change makes this map stale>`

## Decisions

- `<decision>` — `<reason and affected paths>`

Keep credentials and tokens out of this record. Reconstruct it from the checkout when scratch cleanup sweeps it.
