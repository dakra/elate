"""elate — Emacs Lisp Automation Tool."""

from importlib.metadata import PackageNotFoundError, version as _dist_version

try:
    __version__ = _dist_version("elate")
except PackageNotFoundError:  # running from a bare checkout
    __version__ = "0+unknown"
