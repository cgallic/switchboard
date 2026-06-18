"""Switchboard — packaging.

Core runtime is stdlib-only so the offline demo + tests run with zero installs.
Live mode (real Claude extraction + real Notion writes + MCP serving) needs the
optional extras below.
"""
from setuptools import setup, find_packages

setup(
    name="switchboard",
    version="0.1.0",
    description="A gated multi-step agent that turns a live phone call into a "
                "clean, dated, queryable Notion job record.",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="Connor Gallic",
    license="MIT",
    python_requires=">=3.10",
    packages=find_packages(),
    install_requires=[],  # offline demo + tests need nothing beyond Python
    extras_require={
        "live": [
            "anthropic>=0.39",     # Claude extraction
            "notion-client>=2.2",  # real Notion API writes
        ],
        "mcp": [
            "mcp>=1.2",            # serve the four tools over MCP
        ],
    },
    entry_points={
        "console_scripts": [
            "switchboard=switchboard.agent_loop:_main_console",
            "switchboard-mcp=switchboard.notion_mcp:_main",
        ],
    },
)
