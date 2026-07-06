"""cog_loader plugin — generic loader for third-party discord.py cogs.

This plugin is a domain-agnostic loader. It does NOT contain any
business-specific cogs (e.g., no /watch, no salvage, no approval).
It only provides the loading mechanism:

1. Read the configured ``cogs_dir`` (default: ``$HERMES_HOME/cogs``)
2. For each ``.py`` file in that directory:
   a. Import the module via importlib
   b. Look for either a top-level ``register(ctx)`` function OR a
      top-level ``setup(bot, tree)`` callable
   c. Call whichever is present with the plugin's context

This means the loader itself is cogs-agnostic: a contributor drops
their own cog .py file into the configured directory, the loader
discovers + registers it. The loader does not assume any particular
cog's purpose or interface.

The cogs themselves are responsible for:
- Adding their own slash commands to the agent
- Registering their own discord.py views / buttons / events
- Wiring their own config (via ctx.config or env vars)

If you want to add a new cog:
1. Create ``$HERMES_HOME/cogs/my_cog.py``
2. Define either:
   - ``def register(ctx) -> None:`` — uses the agent's plugin context API
   - ``def setup(bot, tree) -> None:`` — uses the raw discord.py API
3. Restart the gateway

Configuration (in ``config.yaml``):
    cog_loader:
        cogs_dir: /opt/hermes/cogs        # default if unset
        # Set to false to disable the loader entirely (default: true)
        enabled: true
        # Per-cog enable/disable list (optional)
        # disabled_cogs: [legacy_cog, broken_cog]
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Resolved at register() time (not at import time, so tests can monkey-patch)
_cogs_dir: Optional[Path] = None
_enabled_cogs: Optional[Set[str]] = None
_disabled_cogs: Optional[Set[str]] = None
_loaded_cogs: List[str] = []  # for diagnostic logging


def _resolve_config(ctx) -> tuple[Path, bool, Optional[Set[str]], Optional[Set[str]]]:
    """Resolve the loader's configuration from ctx.config + env vars.

    The plugin reads its config from the agent's config.yaml under
    ``cog_loader:`` (per the PluginContext.config surface). The env-var
    override (``HERMES_COGS_DIR``) is for tests + out-of-band
    deployments where the config.yaml path isn't convenient.
    """
    enabled = True
    cogs_dir_str: Optional[str] = None
    enabled_set: Optional[Set[str]] = None
    disabled_set: Optional[Set[str]] = None

    # 1. Try the agent's config.yaml first (the canonical path)
    try:
        cfg = ctx.config.get("cog_loader", {}) if hasattr(ctx, "config") else {}
    except Exception:
        cfg = {}

    if isinstance(cfg, dict):
        cogs_dir_str = cfg.get("cogs_dir") or cogs_dir_str
        enabled = bool(cfg.get("enabled", enabled))
        if isinstance(cfg.get("enabled_cogs"), list):
            enabled_set = set(str(x) for x in cfg["enabled_cogs"])
        if isinstance(cfg.get("disabled_cogs"), list):
            disabled_set = set(str(x) for x in cfg["disabled_cogs"])

    # 2. Env-var override
    cogs_dir_str = os.environ.get("HERMES_COGS_DIR") or cogs_dir_str

    # 3. Default: $HERMES_HOME/cogs
    if not cogs_dir_str:
        hermes_home = os.environ.get("HERMES_HOME", "/opt/hermes")
        cogs_dir_str = str(Path(hermes_home) / "cogs")

    return Path(cogs_dir_str), enabled, enabled_set, disabled_set


def _load_cog(cog_path: Path, ctx) -> bool:
    """Load a single cog file + call its register(ctx) or setup(bot, tree).

    Returns True on success, False on failure (with logger.warning).
    """
    cog_name = cog_path.stem
    module_name = f"hermes_cogs_external.{cog_name}"

    # Domain-agnostic module loading via importlib (avoids name collision
    # with any cog that picks a name already in sys.modules)
    spec = importlib.util.spec_from_file_location(module_name, cog_path)
    if spec is None or spec.loader is None:
        logger.warning("cog_loader: %s has no importable spec, skipping", cog_path)
        return False
    try:
        module = importlib.util.module_from_spec(spec)
    except Exception as exc:
        logger.warning("cog_loader: %s failed to allocate module (%s), skipping",
                       cog_path, exc)
        return False

    # Make the cog's relative imports work (if any)
    cog_dir = str(cog_path.parent)
    if cog_dir not in sys.path:
        sys.path.insert(0, cog_dir)

    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        logger.warning("cog_loader: %s raised during exec_module (%s), skipping",
                       cog_path, exc, exc_info=True)
        return False

    # Try the canonical register(ctx) entry point first (per the agent's
    # plugin convention). Fall back to a discord.py-flavored setup(bot, tree)
    # if the cog uses the lower-level API. If neither is present, the cog
    # is malformed (this is the loader being cogs-agnostic, not cogs-knowing).
    register_fn: Optional[Callable] = getattr(module, "register", None)
    setup_fn: Optional[Callable] = getattr(module, "setup", None)

    if register_fn is not None and callable(register_fn):
        try:
            register_fn(ctx)
        except Exception as exc:
            logger.warning("cog_loader: %s.register(ctx) raised (%s), the cog is not loaded",
                           cog_name, exc, exc_info=True)
            return False
        logger.info("cog_loader: loaded %s via register(ctx)", cog_name)
        return True

    if setup_fn is not None and callable(setup_fn):
        # Fallback: the cog uses the raw discord.py API. We need the bot
        # + tree. The agent's discord platform exposes these via
        # ctx.discord_bot / ctx.discord_tree (per the discord platform's
        # PluginContext). If those attributes are missing, the cog is
        # not loadable in this context.
        bot = getattr(ctx, "discord_bot", None)
        tree = getattr(ctx, "discord_tree", None)
        if bot is None or tree is None:
            logger.warning(
                "cog_loader: %s uses setup(bot, tree) but the loader has no "
                "discord bot/tree handle on the context. The cog is not loaded. "
                "(This is expected on non-Discord platforms.)",
                cog_name,
            )
            return False
        try:
            setup_fn(bot, tree)
        except Exception as exc:
            logger.warning("cog_loader: %s.setup(bot, tree) raised (%s), the cog is not loaded",
                           cog_name, exc, exc_info=True)
            return False
        logger.info("cog_loader: loaded %s via setup(bot, tree)", cog_name)
        return True

    # Neither entry point is present — the cog is malformed. The loader
    # is cogs-agnostic: it does not know what to do with a file that has
    # no register(ctx) and no setup(bot, tree). We log a clear error so
    # the cog author can fix it.
    logger.warning(
        "cog_loader: %s has no register(ctx) and no setup(bot, tree). "
        "A cog must define one of these. The cog is not loaded.",
        cog_path,
    )
    return False


def _discover_cogs(cogs_dir: Path) -> List[Path]:
    """Discover .py files in the cogs directory (recursive, depth-1 only).

    We do NOT recurse into subdirectories of cogs_dir — the convention is
    one file = one cog. (Subdirectories are reserved for cog packages
    with their own __init__.py; the loader does not currently support
    those, but the convention is documented here for the future.)
    """
    if not cogs_dir.is_dir():
        return []
    out: List[Path] = []
    for child in sorted(cogs_dir.iterdir()):
        if child.is_file() and child.suffix == ".py" and child.stem != "__init__":
            out.append(child)
    return out


def register(ctx) -> None:
    """Plugin entry point — called by the agent's plugin loader at startup.

    Per the agent's plugin convention: every plugin's ``__init__.py``
    must export a ``register(ctx) -> None`` function. The agent calls
    this function once at startup (per ``hermes_cli/plugins.py:1700-1800``).
    """
    global _cogs_dir, _enabled_cogs, _disabled_cogs, _loaded_cogs

    cogs_dir, enabled, enabled_set, disabled_set = _resolve_config(ctx)
    _cogs_dir = cogs_dir
    _enabled_cogs = enabled_set
    _disabled_cogs = disabled_set
    _loaded_cogs = []

    if not enabled:
        logger.info("cog_loader: disabled via config (cog_loader.enabled = false)")
        return

    if not cogs_dir.is_dir():
        # This is the expected state for most users — no third-party cogs
        # is not an error. Log at info level + return cleanly.
        logger.info(
            "cog_loader: cogs dir %s does not exist (no third-party cogs to load)",
            cogs_dir,
        )
        return

    cogs = _discover_cogs(cogs_dir)
    if not cogs:
        logger.info("cog_loader: no cogs found in %s", cogs_dir)
        return

    logger.info("cog_loader: discovered %d candidate cog(s) in %s", len(cogs), cogs_dir)
    for cog_path in cogs:
        cog_name = cog_path.stem
        if enabled_set is not None and cog_name not in enabled_set:
            logger.info("cog_loader: %s is not in enabled_cogs, skipping", cog_name)
            continue
        if disabled_set is not None and cog_name in disabled_set:
            logger.info("cog_loader: %s is in disabled_cogs, skipping", cog_name)
            continue
        if _load_cog(cog_path, ctx):
            _loaded_cogs.append(cog_name)

    logger.info(
        "cog_loader: loaded %d/%d cogs from %s (%s)",
        len(_loaded_cogs),
        len(cogs),
        cogs_dir,
        ", ".join(_loaded_cogs) if _loaded_cogs else "(none)",
    )
