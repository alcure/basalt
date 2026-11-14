# Alcure workstation updates

Linux administration tools for an existing local Basalt installation managed by the
Alcure Worker and user systemd units. They do not change the engine's supported platforms.
The preserved serving controls are documented in [Alcure controls](../../docs/alcure.md).

## Installation

The storage directory must contain an existing `current` release link and `models` directory.
The workstation must already have `alcure-basalt.service`, the Alcure Worker, Omarchy,
CUDA, CMake, Git, uv and the runtime model configuration installed.

```sh
python tools/alcure_updates/install.py --storage /path/to/basalt
```

`--repository`, `--python` and `--jobs` select the integration checkout, venv interpreter
and compilation concurrency. The installer saves existing files before replacing them.
It configures `upstream` when absent and creates `alcure-auto` at HEAD when absent.
It enables timers for future updates; it does not compile or replace an existing release
during installation. Keep the checkout containing these administration tools in place.

## Preparation

The update timer checks `upstream/main` every six hours. The Omarchy post-update hook
starts the same persistent unit. The GPU and update locks coordinate with the Worker
and the previous workstation updater.

Each candidate is a detached Git worktree based on `alcure-auto`. Upstream is merged
without rebasing local history or choosing a side automatically in conflicts. Candidates
receive separate virtual environments, pinned dependencies, compilation, the complete
server test suite, installed pack reader checks and validation of configured engine flags.
Packs and the user's model configuration are never automatically migrated or rewritten.

Failed candidates retain their worktree and diagnostics. The same failed combination of
upstream, integration and model configuration is held until it changes or an operator runs
`alcure-basalt-update --retry`. Release trees are retained; a build requires 15 GiB free.

## Activation

Activation is retried every ten minutes. It acquires the same GPU lock as the Worker and
Llamarchy, then requests maintenance from the motor. Running or queued requests defer
activation. An active but unloaded motor is left unloaded until normal use loads it again.
A stopped service is not started automatically.

The `current` link is replaced atomically. After restarting the service, activation checks
the loaded state, maintenance capability and exact pack identity, followed by real token
counting and a short generation. Failure restores the previous link and restarts the old
release. A rejected candidate is not restarted in a loop. A compatible configuration change
while staging is revalidated under the GPU lock before activation.

An interrupted activation keeps `activating.json` and blocks later operations for inspection.
The record is also retained if restoring the previous release fails.

## Operation

```sh
basalt-update-status
systemctl --user list-timers 'alcure-basalt-*'
systemctl --user start --no-block alcure-basalt-update.service
systemctl --user start --no-block alcure-basalt-activate.service
```

Configuration: `~/.config/basalt/updates.json`.
Logs: `~/.cache/alcure-basalt-update.log`.
State: `~/.local/state/alcure-basalt-update/`.
`pending.json` identifies a tested candidate; `installed.json` records the latest promotion.
`previous` beside `current` points to the previous release after successful promotion.

To pause periodic checks and activation:

```sh
systemctl --user disable --now alcure-basalt-update.timer alcure-basalt-activate.timer
```

For a complete pause, also move the Omarchy post-update hook outside its hooks directory.

## Integration maintenance

Incompatible upstream lifecycle changes can still require human adaptation. The updater
never discards local controls to make a merge pass. Develop fixes in a separate worktree,
test them, then advance `alcure-auto` to the validated commit. Do not edit an active release.
The tools never push or publish source code automatically.

```sh
python -m unittest discover -s tools/alcure_updates -p 'test_*.py'
```

Tests use real temporary Git repositories and simulated motor and service controls. They
cover local changes surviving integration, conflicts, validation failure, failure retention,
GPU admission, configuration changes, candidate modification, activation and rollback.
