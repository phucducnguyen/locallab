"""Lane E: a fleet of emulated NUCLEO boards (QEMU) beside the real one.

The one-way rule: an emulated board failing the same way as the real board
supports CODE; an emulated board passing says nothing. Nothing here ever
touches the real board, its port or its build output.
"""
