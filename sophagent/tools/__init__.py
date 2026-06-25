"""Import all tool modules so their @tool registrations run."""

from . import files, session_search, terminal, web  # noqa: F401

# memory, skills and delegate register on import too; imported lazily in
# main.py once their dependencies (db, skill store) exist.


def load_all() -> None:
    from . import cron, delegate, memory, skills  # noqa: F401
