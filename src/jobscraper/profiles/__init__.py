"""Search profiles (S1.7 foundation; 01 §35, RUN-03).

Profiles are first-class and all user-specific matching logic is data.
Edits create an immutable ``profile_revision`` before new evaluation work
is planned; the mutable row points at the current revision.
"""
