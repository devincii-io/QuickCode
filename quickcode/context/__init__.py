"""How data is shaped before the model reads it.

Everything in here is about the *model's* view. Nothing in this package may be
used to build an HTTP response, a session record or a config file: those are
JSON on purpose, because other programs read them.
"""
