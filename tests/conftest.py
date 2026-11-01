"""Tests must not read or mutate a developer's persisted traces."""
import os
os.environ['AGENT_TRACER_DB'] = ':memory:'
