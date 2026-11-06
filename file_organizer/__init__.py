"""Intelligent CLI file organizer.

The package is intentionally import-light: submodules are imported lazily so
``import file_organizer`` never requires ``rich``, ``tqdm`` or ``openai``.
"""

__version__ = "1.0.0"
__author__ = "file_organizer contributors"
__all__ = ["__version__", "main"]


def main(argv=None):  # type: (object) -> int
    """Lazy entry point: ``python -c "import file_organizer; file_organizer.main()"``."""
    from .main import main as _main

    return _main(argv)
