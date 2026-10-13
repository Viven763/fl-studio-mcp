"""Plugin parameter tools: find parameters by name, set them by value, number or text."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_params_tools(mcp: FastMCP) -> None:
    from fl_studio_mcp.utils.fl_files import EDIT_LOCK
    from fl_studio_mcp.utils.fl_params import (
        RISKY,
        describe_params,
        is_textual,
        param_names,
        param_options,
        search_params,
        set_params,
    )

    @mcp.tool()
    def fl_find_plugin_params(
        index: int, query: str, slot_index: int = -1, limit: int = 20, options: bool = False
    ) -> dict:
        """Search a plugin's parameters by name and show their current values.

        Works for channel instruments (slot_index -1, index = global channel)
        and mixer effects (index = insert, slot_index = 0-9). Big plugins are
        fine (ZENOLOGY has ~4000 parameters): names are fetched once and cached.

        Args:
            index: Channel index (global), or mixer insert when slot_index >= 0
            query: Words that must all appear in the name, e.g. "porta time", "cutoff"
            slot_index: -1 for a channel's instrument, 0-9 for a mixer effect slot
            limit: Maximum matches to return
            options: Also list the choices of text-valued matches (at most 5 scanned;
                     parameters that act when moved, like randomize/preset, are skipped)
        Returns {"plugin", "param_count", "matches": [{"index", "name", "value",
        "text", "options"?}], "truncated"}.
        """
        with EDIT_LOCK:
            try:
                plugin, names = param_names(index, slot_index)
                hits = search_params(names, query, limit + 1)
                matches = describe_params(index, slot_index, hits[:limit]) if hits else []
                scanned = 0
                if options:
                    for m in matches:
                        if scanned >= 5:
                            break
                        if is_textual(m["text"]) and not RISKY.search(m["name"]):
                            m["options"] = param_options(index, slot_index, m["index"])["options"]
                            scanned += 1
            except (RuntimeError, ValueError) as e:
                return {"error": str(e)}
        return {
            "plugin": plugin,
            "param_count": len(names),
            "matches": matches,
            "truncated": len(hits) > limit,
        }

    @mcp.tool()
    def fl_set_plugin_params(index: int, params: list[dict], slot_index: int = -1) -> dict:
        """Set plugin parameters live (no reload) by index or name.

        Each item names the parameter ("param": index or exact name) and gives
        exactly one of:
            {"param": "Cutoff", "value": 0.5}         # normalized 0..1
            {"param": 7, "target": -3.0}               # the number FL displays (dB, Hz, ms, %)
            {"param": "Band 1 type", "text": "High pass"}   # a label of a text-valued param
        Text values are found by scanning the parameter's range; if no single
        label matches, the value is left unchanged and the options are listed.

        Args:
            index: Channel index (global), or mixer insert when slot_index >= 0
            params: Items as above
            slot_index: -1 for a channel's instrument, 0-9 for a mixer effect slot
        Returns {"params": {"<index>": {"name", "text", "value"}}, "errors": [...]}.
        """
        with EDIT_LOCK:
            try:
                return set_params(index, slot_index, params)
            except (RuntimeError, ValueError) as e:
                return {"error": str(e)}
