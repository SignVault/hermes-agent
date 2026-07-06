"""Tests for the cog_loader plugin (plugins/cog_loader/__init__.py).

The loader is cogs-agnostic by design: the test suite creates small
synthetic cogs in a temp directory + verifies the loader discovers
them, calls the right entry point, and respects the enable/disable
configuration.

Coverage:
  - test_register_with_register_entry_point    (happy path: register(ctx))
  - test_register_with_setup_entry_point       (fallback: setup(bot, tree))
  - test_register_with_neither_entry_point     (malformed cog -> warning, not crash)
  - test_register_with_register_raising        (cog raises -> skipped + others continue)
  - test_register_isolated_failures            (multi-cog partial success)
  - test_register_with_no_cogs_dir             (missing dir -> clean info log)
  - test_register_with_empty_cogs_dir          (dir but no .py -> clean info log)
  - test_register_skips_dunder_init            (__init__.py is NOT a cog)
  - test_register_loads_multiple_cogs          (multi-cog discovery, sorted)
  - test_register_respects_disabled_cogs       (per-cog disable list)
  - test_register_respects_enabled_cogs        (per-cog enable whitelist)
  - test_register_respects_enabled_false       (master disable flag)
  - test_plugin_yaml_manifest_is_valid         (manifest fields + domain-agnostic)
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_cog(cog_dir: Path, name: str, source: str) -> Path:
    """Write a single .py file in cog_dir + return the path."""
    cog_dir.mkdir(parents=True, exist_ok=True)
    cog_path = cog_dir / f"{name}.py"
    cog_path.write_text(source)
    return cog_path


def _make_ctx(cog_dir: Path, *, enabled: bool = True,
              enabled_cogs=None, disabled_cogs=None):
    """Build a minimal ctx that the loader can call into.

    Mirrors the agent's config.yaml schema: the loader reads from
    ``ctx.config["cog_loader"]`` (the ``cog_loader:`` stanza in the
    agent's config.yaml). The test sets the nested dict accordingly.
    """
    ctx = MagicMock()
    inner = {
        "cogs_dir": str(cog_dir),
        "enabled": enabled,
    }
    if enabled_cogs is not None:
        inner["enabled_cogs"] = enabled_cogs
    if disabled_cogs is not None:
        inner["disabled_cogs"] = disabled_cogs
    ctx.config = {"cog_loader": inner}
    # The optional discord handles default to None (the setup() fallback
    # path logs its expected warning if it gets there; we verify that below).
    ctx.discord_bot = None
    ctx.discord_tree = None
    return ctx


PLUGIN_ROOT = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "plugins" / "cog_loader"
)


def _reload_loader_module():
    """Re-import the loader so each test gets a clean module state.

    The loader stores resolved-config globals at module level (per
    design -- see the global declarations at the top of __init__.py);
    we reset them via re-import so test ordering does not matter.
    """
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location(
        "cog_loader_under_test", PLUGIN_ROOT / "__init__.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not build importlib spec for {PLUGIN_ROOT / '__init__.py'}")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_register_with_register_entry_point(tmp_path, caplog):
    """A cog with register(ctx) is loaded + its register(ctx) is called.

    Verification strategy: the cog writes a marker file when register
    runs. The test reads the marker file after the loader finishes. This
    works regardless of how the loader manages its module namespace
    (sys.modules, in-memory objects, etc.).
    """
    marker = tmp_path / "marker.txt"
    _write_cog(
        tmp_path,
        "happy",
        f"def register(ctx):\n"
        f"    with open({str(marker)!r}, 'w') as f:\n"
        f"        f.write(repr(ctx))\n",
    )
    ctx = _make_ctx(tmp_path)
    mod = _reload_loader_module()
    with caplog.at_level(logging.INFO):
        mod.register(ctx)

    # The cog's register(ctx) was called -- it wrote the marker file
    assert marker.exists(), (
        "cog's register(ctx) was not called (no marker file written)"
    )
    # The marker file's contents include a MagicMock repr (the test's ctx).
    # We don't assert on the exact contents (the repr is unstable across
    # unittest.mock versions); we only assert that the marker was written.

    # The loader logs success
    assert "cog_loader: loaded happy via register(ctx)" in caplog.text
    assert "cog_loader: loaded 1/1 cogs" in caplog.text


def test_register_with_setup_entry_point(tmp_path, caplog):
    """A cog with setup(bot, tree) is loaded when ctx provides bot+tree.

    Same verification strategy as register: the cog writes a marker file
    when setup runs. The marker records the bot + tree handles so the
    test can assert they were passed through correctly.
    """
    marker = tmp_path / "marker.txt"
    _write_cog(
        tmp_path,
        "raw",
        f"def setup(bot, tree):\n"
        f"    with open({str(marker)!r}, 'w') as f:\n"
        f"        f.write(repr(bot) + '|' + repr(tree))\n",
    )
    ctx = _make_ctx(tmp_path)
    ctx.discord_bot = MagicMock(name="bot")
    ctx.discord_tree = MagicMock(name="tree")
    mod = _reload_loader_module()
    with caplog.at_level(logging.INFO):
        mod.register(ctx)

    # The cog's setup(bot, tree) was called -- it wrote the marker file.
    assert marker.exists(), (
        "cog's setup(bot, tree) was not called (no marker file written)"
    )
    assert "cog_loader: loaded raw via setup(bot, tree)" in caplog.text


# ---------------------------------------------------------------------------
# Malformed cogs (graceful failure, not crashes)
# ---------------------------------------------------------------------------

def test_register_with_neither_entry_point(tmp_path, caplog):
    """A cog with no register and no setup logs a clear warning + is skipped."""
    _write_cog(tmp_path, "malformed", "# No register, no setup\nX = 1\n")
    ctx = _make_ctx(tmp_path)
    mod = _reload_loader_module()
    with caplog.at_level(logging.INFO):
        mod.register(ctx)
    # The warning names the malformed file + explains what's wrong
    assert "has no register(ctx) and no setup(bot, tree)" in caplog.text
    assert "A cog must define one of these" in caplog.text
    assert "loaded 0/1 cogs" in caplog.text


def test_register_with_register_raising(tmp_path, caplog):
    """A cog whose register() raises is skipped + the loader continues."""
    _write_cog(tmp_path, "broken", "def register(ctx):\n"
                                   "    raise RuntimeError('intentional test failure')\n")
    ctx = _make_ctx(tmp_path)
    mod = _reload_loader_module()
    with caplog.at_level(logging.INFO):
        mod.register(ctx)
    assert "broken.register(ctx) raised (intentional test failure)" in caplog.text
    assert "loaded 0/1 cogs" in caplog.text


def test_register_isolated_failures(tmp_path, caplog):
    """One cog's failure does NOT block other cogs from loading."""
    _write_cog(tmp_path, "good", "def register(ctx): pass\n")
    _write_cog(tmp_path, "broken", "def register(ctx):\n"
                                    "    raise RuntimeError('boom')\n")
    ctx = _make_ctx(tmp_path)
    mod = _reload_loader_module()
    with caplog.at_level(logging.INFO):
        mod.register(ctx)
    assert "loaded good" in caplog.text
    assert "broken.register(ctx) raised (boom)" in caplog.text
    assert "loaded 1/2 cogs" in caplog.text


# ---------------------------------------------------------------------------
# Missing / empty directories (graceful no-ops)
# ---------------------------------------------------------------------------

def test_register_with_no_cogs_dir(tmp_path, caplog):
    """A non-existent cogs dir logs at INFO + returns cleanly (not an error)."""
    nonexistent = tmp_path / "nope"  # never created
    ctx = _make_ctx(nonexistent)
    mod = _reload_loader_module()
    with caplog.at_level(logging.INFO):
        mod.register(ctx)
    # The exact wording of the log is part of the contract (operators
    # + downstream log scrapers depend on it). The loader logs:
    #     "cogs dir <path> does not exist (no third-party cogs to load)"
    assert "does not exist" in caplog.text
    assert "no third-party cogs to load" in caplog.text


def test_register_with_empty_cogs_dir(tmp_path, caplog):
    """An empty cogs dir is a clean no-op."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx(tmp_path)
    mod = _reload_loader_module()
    with caplog.at_level(logging.INFO):
        mod.register(ctx)
    assert "no cogs found" in caplog.text


def test_register_skips_dunder_init(tmp_path):
    """An __init__.py file in the cogs dir is NOT loaded as a cog."""
    (tmp_path / "__init__.py").write_text("X = 1\n")
    _write_cog(tmp_path, "real", "def register(ctx): pass\n")
    ctx = _make_ctx(tmp_path)
    mod = _reload_loader_module()
    mod.register(ctx)
    # Only the real cog is loaded, not __init__
    assert "real" in mod._loaded_cogs
    assert len(mod._loaded_cogs) == 1


# ---------------------------------------------------------------------------
# Multi-cog discovery
# ---------------------------------------------------------------------------

def test_register_loads_multiple_cogs(tmp_path):
    """Multiple cogs in the same dir are all loaded in sorted order."""
    for name in ("zeta", "alpha", "mu"):
        _write_cog(tmp_path, name, "def register(ctx): pass\n")
    ctx = _make_ctx(tmp_path)
    mod = _reload_loader_module()
    mod.register(ctx)
    # Sorted alphabetically (per _discover_cogs)
    assert mod._loaded_cogs == ["alpha", "mu", "zeta"]


# ---------------------------------------------------------------------------
# Configuration: enable/disable
# ---------------------------------------------------------------------------

def test_register_respects_disabled_cogs(tmp_path):
    """A cog in disabled_cogs is skipped."""
    _write_cog(tmp_path, "a", "def register(ctx): pass\n")
    _write_cog(tmp_path, "b", "def register(ctx): pass\n")
    ctx = _make_ctx(tmp_path, disabled_cogs=["b"])
    mod = _reload_loader_module()
    mod.register(ctx)
    assert mod._loaded_cogs == ["a"]


def test_register_respects_enabled_cogs(tmp_path):
    """When enabled_cogs is set, only whitelisted cogs are loaded."""
    _write_cog(tmp_path, "a", "def register(ctx): pass\n")
    _write_cog(tmp_path, "b", "def register(ctx): pass\n")
    _write_cog(tmp_path, "c", "def register(ctx): pass\n")
    ctx = _make_ctx(tmp_path, enabled_cogs=["a", "c"])
    mod = _reload_loader_module()
    mod.register(ctx)
    assert mod._loaded_cogs == ["a", "c"]


def test_register_respects_enabled_false(tmp_path):
    """When enabled=false, the loader is a no-op (even if cogs exist)."""
    _write_cog(tmp_path, "a", "def register(ctx): pass\n")
    ctx = _make_ctx(tmp_path, enabled=False)
    mod = _reload_loader_module()
    mod.register(ctx)
    assert mod._loaded_cogs == []


# ---------------------------------------------------------------------------
# Plugin manifest
# ---------------------------------------------------------------------------

def test_plugin_yaml_manifest_is_valid():
    """The plugin.yaml has the required fields for the plugin loader to find it."""
    content = (PLUGIN_ROOT / "plugin.yaml").read_text()
    assert "name: cog-loader" in content
    assert "kind: extension" in content
    # The loader is cogs-agnostic -- the manifest must not embed any
    # business-specific cog names.
    for business_term in ("watch", "salvage", "approval", "alpaca"):
        assert business_term not in content.lower(), (
            f"plugin.yaml must be domain-agnostic; found business term '{business_term}'"
        )