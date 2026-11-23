"""The background worker loop and the processing steps for one ticket (section 7).

A worker claims a pending ticket, scores it for prompt injection, calls the
model, validates the reply, and records the result. The order of those steps
(section 7.4) is deliberate and must not change.
"""
