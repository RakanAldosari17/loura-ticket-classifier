"""The model boundary: building the prompt, and validating what comes back.

Prompt building keeps the instructions and the ticket text separate (section
10.2). Output validation (section 8.3) decides whether a reply is allowed to be
stored at all: the model returns text, never truth.
"""
