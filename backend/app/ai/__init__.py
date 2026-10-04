"""
Provider-agnostic AI platform layer.

Knows how to build and call chat models; knows nothing about energy.
Feature modules (e.g. `app.modules.assistant`) depend on this package, never
the other way around.
"""
