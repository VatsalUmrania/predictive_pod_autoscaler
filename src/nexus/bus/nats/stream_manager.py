"""Stream creation and subject overlap resolution for JetStream."""
from __future__ import annotations

import logging

from nats.js import JetStreamContext
from nats.js.api import StreamConfig
from nats.js.errors import BadRequestError

logger = logging.getLogger(__name__)


async def ensure_stream(js: JetStreamContext | None, config: StreamConfig, name: str) -> None:
    """Ensure a JetStream stream exists with the correct configuration."""
    if js is None:
        raise RuntimeError("[NATS] JetStream is not connected")

    try:
        existing_info = await js.stream_info(name)
        existing_subjects = list(existing_info.config.subjects or [])
        desired_subjects = list(config.subjects or [])
        if existing_subjects != desired_subjects:
            logger.warning(
                f"[NATS] Stream '{name}' has drifted subjects {existing_subjects} → {desired_subjects}. Updating."
            )
        try:
            await js.update_stream(config)
            logger.info(f"[NATS] Stream '{name}' config reconciled")
        except BadRequestError as upd_exc:
            logger.debug(f"[NATS] Stream '{name}' update no-op: {upd_exc}")
        return
    except Exception as find_exc:
        exc_str = str(find_exc)
        if "10059" not in exc_str and "not found" not in exc_str.lower():
            logger.warning(f"[NATS] stream_info('{name}') raised: {find_exc}")

    try:
        await js.add_stream(config)
        logger.info(f"[NATS] Created stream '{name}'")
        return
    except BadRequestError as exc:
        exc_str = str(exc)
        if "10058" in exc_str or "already in use" in exc_str:
            try:
                await js.update_stream(config)
                logger.info(f"[NATS] Updated stream '{name}' to current config")
                return
            except Exception as upd_exc:
                raise RuntimeError(
                    f"[NATS] Stream '{name}' exists with wrong config and update failed: {upd_exc}"
                ) from upd_exc

        if "10065" in exc_str or "subjects overlap" in exc_str:
            await resolve_subject_overlap(js, config, name)
            return

        raise RuntimeError(f"[NATS] Stream '{name}' creation failed: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"[NATS] Stream '{name}' creation failed unexpectedly: {exc}") from exc


async def resolve_subject_overlap(js: JetStreamContext | None, config: StreamConfig, name: str) -> None:
    """Evict overlapping subjects from competing stream and retry creation."""
    if js is None:
        raise RuntimeError("[NATS] JetStream is not connected")

    desired_subjects = list(config.subjects or [])
    assert desired_subjects, f"[NATS] Config for '{name}' has no subjects — cannot resolve overlap"

    for subject in desired_subjects:
        try:
            owner_name = await js.find_stream_name_by_subject(subject)
        except Exception:
            continue

        if owner_name == name:
            continue

        logger.warning(
            f"[NATS] Subject '{subject}' is claimed by stream '{owner_name}' instead of '{name}'. Evicting."
        )
        try:
            owner_info = await js.stream_info(owner_name)
            current_subjects: list[str] = list(owner_info.config.subjects or [])
            corrected = [s for s in current_subjects if s not in desired_subjects]
            if not corrected:
                raise RuntimeError(
                    f"[NATS] Resolving subject overlap would leave stream '{owner_name}' with no subjects."
                )
            owner_info.config.subjects = corrected
            await js.update_stream(owner_info.config)
            logger.info(
                f"[NATS] Evicted overlapping subjects from '{owner_name}': {current_subjects} → {corrected}"
            )
        except RuntimeError:
            raise
        except Exception as fix_exc:
            raise RuntimeError(
                f"[NATS] Could not evict overlapping subjects from '{owner_name}': {fix_exc}"
            ) from fix_exc

    try:
        await js.add_stream(config)
        logger.info(f"[NATS] Created stream '{name}' after resolving subject overlap")
    except Exception as retry_exc:
        raise RuntimeError(
            f"[NATS] Stream '{name}' still failed after subject overlap resolution: {retry_exc}"
        ) from retry_exc
