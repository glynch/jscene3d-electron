#!/usr/bin/python3
"""Shared failure type for guarded Electron build operations."""


class BuildSafetyError(RuntimeError):
    """Raised when an operation could endanger the native build output."""
