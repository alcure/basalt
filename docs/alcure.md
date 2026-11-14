# Alcure controls

Local integration preserves maintenance reservations, active pack metadata and visual token counting.

## Maintenance

`POST /v1/unload` with `{"reserve": true}` closes admission and unloads the selected model.
It returns `status: reserved` and `maintenance: true` only after unloading finishes.
A running, queued, loading or already reserved model returns 409 without taking over its reservation.
`{"reserve": false}` reopens admission after unloading has finished. Without `reserve`, unloading
keeps its ordinary behavior. The optional `model` selects the model, as on `/v1/load`.

New inference, token counting and model load requests return 409 while a reservation stands.
The router's startup preload and internal model loads respect the same reservation.
Clients must release a reservation they own after changing the pack or restart the motor.

## State

`/health` and `/v1/status` publish `control_features: ["maintenance_reservation"]`, `maintenance`
and `pack`. The pack identity is `filename`, `size_bytes` and `mtime_ns` from the engine's actual
model path. It is null when that file cannot be inspected; it is never inferred from configuration.

## Implementation

`serve/alcure.py` owns the reservation state, router reservation helper and multimodal token counting.
`Service` initializes the controls, guards loading and includes their state in status.
The handler admits model work before reading a request body and releases admission in `finally`.
These explicit call sites must remain integrated when the upstream request lifecycle changes.
`serve/tests/test_alcure_controls.py` tests reservation refusal, release, concurrency and pack identity.

## Token budgets

`POST /v1/messages/count_tokens` counts the rendered prompt, including the expanded visual rows for every image.
Text-only counting does not load the model. Counting images requires the vision-enabled engine to be loaded and
runs the image encoder, but does not generate a reply or consume a decode slot. The encoder cache is reused by a
subsequent generation; the count request's combined embedding file is removed when counting ends. Clients must
send the same images and system text for counting and generation rather than budgeting image markers as text.

Set `model` to a configured alias or model file name, as for generation. An unknown name returns 404. Counts use that
model's prompt format and tokenizer; use the same model for counting and generation. Image counting may wait for a
model swap before the selected engine can run the encoder. See [models and the router](configuration/models.md).
