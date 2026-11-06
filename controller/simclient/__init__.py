"""Typed HTTP client for the simulator (DESIGN.md 11.2, 8.4).

Owner: M0. `from controller.simclient import SimClient` and the error classes.
"""
from controller.simclient.client import SimClient
from controller.simclient.errors import *  # noqa: F401,F403  (the typed error classes)
