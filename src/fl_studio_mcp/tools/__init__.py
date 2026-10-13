"""FL Studio MCP tools."""

from fl_studio_mcp.tools.audio import register_audio_tools
from fl_studio_mcp.tools.channels import register_channel_tools
from fl_studio_mcp.tools.extended import register_extended_tools
from fl_studio_mcp.tools.mixer import register_mixer_tools
from fl_studio_mcp.tools.params import register_params_tools
from fl_studio_mcp.tools.piano_roll import register_piano_roll_tools
from fl_studio_mcp.tools.plugins import register_plugin_tools
from fl_studio_mcp.tools.production import register_production_tools
from fl_studio_mcp.tools.project import register_project_tools
from fl_studio_mcp.tools.transport import register_transport_tools

__all__ = [
    "register_transport_tools",
    "register_mixer_tools",
    "register_channel_tools",
    "register_plugin_tools",
    "register_piano_roll_tools",
    "register_extended_tools",
    "register_audio_tools",
    "register_production_tools",
    "register_params_tools",
    "register_project_tools",
]
