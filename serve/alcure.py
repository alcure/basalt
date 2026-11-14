"""Alcure pack maintenance and multimodal token budgets.

Service calls init_controls, guards loading and publishes control_status. The HTTP
handler admits work before reading its body and releases it in a finally block.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from serve.engine.errors import ModelBusy
from serve.frontend import anthropic_to_messages, forced_call, media_of
from serve.util.text import stop_strings

CONTROL_FEATURES = ("maintenance_reservation",)
# Admission begins before the body identifies a model, so it covers every service.
WORK_PATHS = ("/v1/responses", "/v1/chat/completions", "/v1/messages", "/v1/messages/count_tokens",
              "/v1/vram", "/v1/load", "/load")


class MaintenanceControls:
    def init_controls(self):
        self.admission = threading.Lock()             # guards the work count and the maintenance flag
        self.work_active = 0                          # work requests admitted and not yet finished
        self.maintenance = False                      # a reservation holds the door shut for a pack swap
        self.reserving = False                        # a reservation is unloading right now

    def pack_info(self) -> dict | None:
        """The real pack this server serves: file name, size and mtime of the loaded file.

        It comes from the engine's own model path (the file it was given), never from
        a configuration file, and only the file's metadata is read: the pack's bytes
        are never opened.
        """
        path = getattr(self.engine, "model_path", None)
        if not isinstance(path, str) or not path:
            return None
        try:
            info = os.stat(path)
        except OSError:
            return None
        return {"filename": os.path.basename(os.path.realpath(path)),
                "size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns}

    def admit_work(self) -> bool:
        """Enter the work section: a request that will use the model or the GPU.

        False when a maintenance reservation holds the admission shut: the request
        is refused before it touches the model.
        """
        with self.admission:
            if self.maintenance:
                return False
            self.work_active += 1
            return True

    def end_work(self) -> None:
        """Leave the work section (every admitted request leaves it, error or not)."""
        with self.admission:
            self.work_active -= 1

    def reserve_maintenance(self) -> str:
        """Close the admission and unload between requests, so the pack file can be swapped.

        "reserved" when the door is shut and the weights are unloaded; "busy" when a
        request is admitted or the model is loading or booting, when another reservation
        stands or is being established; "unsupported" when the engine cannot be unloaded
        at all. A reservation that fails leaves the server exactly as it was: the door is
        opened again and nothing is pretended. A standing reservation is never taken
        over by another attempt.
        """
        result = self.begin_reservation()
        return self.finish_reservation() if result == "reserving" else result

    def begin_reservation(self) -> str:
        """Close admission without unloading; a router can call this while holding its lock."""
        with self.admission:
            if self.maintenance:
                # A reservation stands: this one does not take it over.
                return "busy"
            if self.reserving:
                return "busy"                     # another reservation is still unloading
            if self.work_active or self.loading or self.booting:
                return "busy"
            self.maintenance = True               # the door is shut from here on
            self.reserving = True                 # ... and it is still being established
        return "reserving"

    def finish_reservation(self) -> str:
        """Unload and complete a reservation begun by begin_reservation()."""
        try:
            result = self.unload()
        except BaseException:
            with self.admission:
                self.maintenance = False
                self.reserving = False
            raise
        with self.admission:
            self.reserving = False
            failed = result in ("busy", "unsupported")
            if failed:
                self.maintenance = False          # the weights are still there: no reservation is left
        if failed:
            return result
        return "reserved"

    def release_maintenance(self) -> bool:
        """Reopen admission only after the reservation has finished unloading."""
        with self.admission:
            if self.reserving:
                return False
            self.maintenance = False
            return True

    def control_status(self):
        return {"control_features": list(CONTROL_FEATURES),
                "maintenance": self.maintenance, "pack": self.pack_info()}


def reserve_pack(router, target):
    """Close admission under the router lock, then unload without holding it."""
    with router.cv:
        if (router.swap or router.queue or router.running or
                any(service.maintenance for service in router.services)):
            raise ModelBusy("a request is running or queued")
        result = target.begin_reservation()
    return target.finish_reservation() if result == "reserving" else result


def count_prompt(handler, req):
    messages, tools, kw = anthropic_to_messages(req, handler.svc.anthropic_think_unasked)
    tools, _ = forced_call(req.get("tool_choice"), tools, handler.svc.dialect.forced_opening)
    stop_strings(req)
    if media_of(messages):
        handler._take_turn()
        handler.svc.load()
        try:
            ids, _, _ = handler.svc.prepare(messages, tools, kw, 1, image_access=handler._image_access())
            count = len(ids)
        finally:
            path = getattr(handler.svc.embeddings, "path", None)
            if path:
                Path(path).unlink(missing_ok=True)
                handler.svc.embeddings.path = None
    else:
        count = len(handler.svc.encode_prompt(messages, tools, kw))
    return count
