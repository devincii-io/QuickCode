"""File checkpoints: what the agent's file edits replaced, and putting it back.

``store``     the per-conversation index and content-addressed blobs on disk
``snapshot``  what a path holds right now
``recorder``  the turn counter, and the before/after bracket around one call
``hook``      the ``LoopHook`` that brackets every file-writing tool call
``rewind``    listing, preview (diffs and conflicts) and the rewind itself
``diff``      line counts and unified diffs over raw bytes
``paths``     project-relative paths, and the refusal to follow a link
``events``    the ``checkpoint`` and ``files_rewound`` session-log records

docs/CHECKPOINTS.md is the user-facing description.
"""
