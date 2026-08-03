from __future__ import annotations


def run_stream_id(agent_name: str, run_id: str) -> str:
    """Return the canonical stream identity for one agent-owned run."""
    if not agent_name or not run_id:
        raise ValueError("agent_name and run_id must not be empty")
    return f"{agent_name}:{run_id}"


def agent_owns_stream(agent_name: str, stream_id: str) -> bool:
    """Check ownership without consulting mutable event payload data."""
    if not agent_name or not stream_id:
        return False
    owner, separator, _ = stream_id.partition(":")
    return separator == ":" and owner == agent_name
