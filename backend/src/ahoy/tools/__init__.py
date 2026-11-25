"""Agent tools. `list_dir`, `read_file` and `search` read the snapshot directly."""


class ToolError(Exception):
    """A tool call failed for a reason other than a refused path (see PathError)."""
