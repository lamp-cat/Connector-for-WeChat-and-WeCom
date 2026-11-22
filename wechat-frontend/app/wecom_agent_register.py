"""Reuse registration without mutating the personal registration module's globals."""
import importlib.util
import re
import sys
from pathlib import Path

from . import agent_register
from .settings import settings

spec = importlib.util.spec_from_file_location("wecom_registration_impl", agent_register.__file__)
implementation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(implementation)
implementation.SERVER = "wecom-reader"
implementation.TOML_BEGIN = "# >>> wecom-reader (消息工作台 自动配置，只读 MCP) >>>"
implementation.TOML_END = "# <<< wecom-reader (消息工作台 自动配置) <<<"
implementation._TOML_OURS = re.compile(r"\n?" + re.escape(implementation.TOML_BEGIN) + r".*?" +
                                     re.escape(implementation.TOML_END) + r"\n?", re.S)
implementation._TOML_HDR = re.compile(r'^\s*\[\s*mcp_servers\s*\.\s*(?:"wecom-reader"|wecom-reader)\s*\]', re.M)
implementation.state_file = lambda: implementation.state_dir() / "wecom-agents.json"
implementation.server_entry = lambda bin_path, cfg_dir: {
    "command": sys.executable, "args": [str(Path(__file__).with_name("mcp_stdio.py")), "--source", "wecom"],
    "env": {"WF_CONFIG": str(settings.config_path)}}
