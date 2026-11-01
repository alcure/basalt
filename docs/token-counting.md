# Token counting

`POST /v1/messages/count_tokens` counts the rendered prompt, including the expanded visual rows for every image.
Text-only counting does not load the model. Counting images requires the vision-enabled engine to be loaded and
runs the image encoder, but does not generate a reply or consume a decode slot. The encoder cache is reused by a
subsequent generation; the count request's combined embedding file is removed when counting ends. Clients must
send the same images and system text for counting and generation rather than budgeting image markers as text.

Set `model` to a configured alias or model file name, as for generation. An unknown name returns 404. Counts use that
model's prompt format and tokenizer; use the same model for counting and generation. Image counting may wait for a
model swap before the selected engine can run the encoder. See [models and the router](configuration/models.md).
