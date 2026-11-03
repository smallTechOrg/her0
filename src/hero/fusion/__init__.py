"""Fusion: perception schemas, view payloads, extraction -> ProjectedBatch conversion, render, critique.

S3b owns ``schemas, views, convert, render, critique``; the orchestrator modules (``state, steps, runner,
publish, assets``) belong to S3c. Importing this package pulls in nothing heavy.
"""
