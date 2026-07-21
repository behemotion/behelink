"""behelink — hosted NAT rendezvous service for the BEHEMOTION harness."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("behelink")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0"
