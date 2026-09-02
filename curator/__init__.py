"""Curator — rank what you've watched.

A comparison-driven rating system for films, TV, anime and documentaries, built
on the batch Bradley-Terry scorer from Archivist's face-rank system. Each media
type is its own contest (see `media_types.py`); the elicitation is "order these
six, ties allowed"; the score is a MAP fit over the whole comparison history,
refit after every round.
"""

__version__ = '1.0.0'
