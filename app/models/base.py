"""The `ModelClient` interface and the model-unavailable error (section 10.1).

This is the only interface in the project. Everything that classifies a ticket
depends on this and nothing else, so the model can be swapped without touching
the workers.
"""
