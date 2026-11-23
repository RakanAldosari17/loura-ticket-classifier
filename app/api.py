"""The front door: the HTTP routes and the single error format (section 6).

Submit a ticket, get one ticket, list tickets. This module never calls the
model: it only validates input, reads and writes the database, and replies.
"""
