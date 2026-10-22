"""Session-id minting: the ONE place that knows the ``YYYYMMDD_HHMMSS_<hex>`` shape.

stdlib-only on purpose: ``agent/``, ``cli.py``, ``gateway/`` and ``tui_gateway/`` all mint ids and
must not pull the store's import graph in to do it. ``hermes_cli/session_lost_and_found.py``
classifies schema-less salvage rows by ``is_known_session_id``, so a shape change here is a
recovery-classification change: keep the prefix stable, and add any NEW derived-id shape to
``SESSION_ID_RECOGNIZERS`` or salvage will drop those sessions and mis-infer the table's layout.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Optional

SESSION_ID_PATTERN = re.compile(r"^\d{8}_\d{6}_")
# Optional profile tag suffix minted for named profiles: ``<minted_id>-<profile_name>``.
# Fixed-width minted prefix keeps the timestamp parseable (system_prompt ``^`` anchor) and the
# suffix unambiguous even though PROFILE_ID_RE allows hyphens inside profile names.
PROFILE_TAG_SUFFIX_RE = re.compile(r"-(?P<tag>[a-z0-9][a-z0-9_-]{0,63})$")

# Ids that surfaces derive instead of minting via new_session_id; salvage uses them as layout
# sentinels, so anchor each on what its mint site fixes.
SESSION_ID_RECOGNIZERS = (
    SESSION_ID_PATTERN,
    # cron/scheduler.py: f"cron_{job_id}_{now:%Y%m%d_%H%M%S}". job_id is uuid4().hex[:12] for jobs
    # minted by cron/jobs.py, but user/legacy job ids ("job-1") are real and still in live stores.
    re.compile(r"^cron_[A-Za-z0-9][A-Za-z0-9._-]*_\d{8}_\d{6}$"),
    # /bg: CLI + gateway mint f"bg_{now:%H%M%S}_{hex6}" (time only, no date); the TUI/Desktop
    # (tui_gateway/methods_prompt.py _side_agent_args) mints f"bg_{uuid4().hex[:6]}".
    re.compile(r"^bg_(?:\d{6}_)?[0-9a-f]{6}$"),
    # gateway/platforms/api_server_room_dispatch.py: f"room_{sha256(seed)[:32]}".
    re.compile(r"^room_[0-9a-f]{32}$"),
    # gateway/platforms/api_server.py: POST /api/sessions + fork f"api_{int(time())}_{hex8}",
    # chat-completions _derive_chat_session_id f"api-{sha256[:16]}".
    re.compile(r"^api_\d{9,}_[0-9a-f]{8}$"),
    re.compile(r"^api-[0-9a-f]{16}$"),
    # gateway/platforms/api_server_runs.py: /v1/runs falls back to its f"run_{uuid4().hex}".
    re.compile(r"^run_[0-9a-f]{32}$"),
    # acp_adapter/session.py and /v1/responses: str(uuid4()).
    re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"),
)


def is_known_session_id(value: str) -> bool:
    """True when ``value`` has the shape of an id one of this repo's surfaces really mints.

    The recognition side of the minting contract. Salvage uses this (not
    ``SESSION_ID_PATTERN`` alone) to tell a session id from an arbitrary cell.
    """
    return any(pattern.match(value) for pattern in SESSION_ID_RECOGNIZERS)

# Interactive surfaces (CLI, TUI, agent, branches, imports) share 6 hex chars: the Desktop's
# session-id candidate regex is pinned to that width. Gateway keys are 8, portability imports 12
# (many rows minted in the same second).
DEFAULT_HEX_LEN = 6


def _active_profile_tag() -> str:
    """Profile name suffix for ids minted inside a named profile, else ``""`` (default profile).

    Lazy import keeps this module importable stdlib-only (the minting contract above);
    hermes_constants is a leaf module, not the SessionDB graph. Tag only applies to named
    profiles under ``<home>/profiles/<name>`` — the default profile's ids stay untouched.
    """
    try:
        from hermes_constants import PROFILE_ID_RE, get_hermes_home
        home = get_hermes_home()
        if home.parent.name == "profiles" and home.name != "default" and PROFILE_ID_RE.match(home.name):
            return home.name
    except Exception:
        pass
    return ""


def new_session_id(now: Optional[datetime] = None, *, hex_len: int = DEFAULT_HEX_LEN) -> str:
    """``<timestamp>_<random hex>[-<profile>]`` for a fresh session; ``now`` pins the timestamp to a
    clock the caller already captured (``agent.session_start``) so the id and the row agree to the
    second. Named profiles append ``-<profile_name>`` so session keys are attributable at a glance
    in cross-profile views (gateway registry, resume hints); the default profile mints bare ids."""
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    tag = _active_profile_tag()
    suffix = f"-{tag}" if tag else ""
    return f"{stamp}_{uuid.uuid4().hex[:hex_len]}{suffix}"
