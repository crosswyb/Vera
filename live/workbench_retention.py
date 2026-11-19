"""Retired maintenance API: meeting data never expires automatically.

Only the authenticated and explicitly confirmed meeting deletion flow removes data.
Old callers are harmless; this function does not inspect or mutate files or records.
"""


def purge(workbench, now=None, apply=False):
    return {"applied": False, "expired_meetings": [], "registered_files_removed": 0,
            "automatic_deletion": False}
