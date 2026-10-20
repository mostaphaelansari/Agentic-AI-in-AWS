"""Select the AgentCore backend configured for an architecture."""

from __future__ import annotations

import logging

from backends.errors import is_authentication_error, public_error_message
from backends.harness import invoke_harness
from backends.runtime import invoke_runtime

logger = logging.getLogger("unified-ui.backends.dispatcher")


def dispatch(
    config,
    session_id,
    user_msg,
    event_queue,
    options=None,
    *,
    controller=None,
    arch_name=None,
):
    try:
        if config["service"] == "harness":
            invoke_harness(
                config,
                session_id,
                user_msg,
                event_queue,
                options,
                controller=controller,
                arch_name=arch_name,
            )
        else:
            invoke_runtime(
                config,
                session_id,
                user_msg,
                event_queue,
                options,
                controller=controller,
                arch_name=arch_name,
            )
    except Exception as exc:
        if is_authentication_error(exc):
            logger.warning("AgentCore request blocked by unavailable AWS credentials")
        else:
            logger.exception("AgentCore backend request failed")
        if controller is not None and controller.cancelled.is_set():
            event_queue.put_nowait(("elapsed", 0))
            event_queue.put_nowait(("done", None))
            return
        event_queue.put_nowait(("event", {"type": "error", "content": public_error_message(exc)}))
        event_queue.put_nowait(("elapsed", 0))
        event_queue.put_nowait(("done", None))
