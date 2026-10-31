"""Jinlin Code Engineer Agent — a code harness, not a demo.

Package layout::

    jinlin_agent/
      cli.py        command line entry
      config.py     layered config (project may only tighten)
      domain.py     message / tool-call / plan / fact / verification data model
      errors.py     stable error codes
      events.py     append-only event log (single source of truth)
      state.py      event folding -> SessionState
      artifacts.py  large payloads, snapshots, reports
      session.py    session lifecycle, locking, resume
      loop.py       agent loop + dispatch + recovery
      context.py    content-aware context construction (T3)
      prompt.py     prompt assembly with provenance
      model/        provider protocol, DeepSeek-compatible, scripted mock
      tools/        registry, fs, patch, shell, git, task
      policy/       path / command / approval
      sandbox/      Windows L1 sandbox, null fallback
      verify.py     verification ladder, baseline, completion gate
      report.py     evidence-bound final report
"""

__version__ = "0.1.0"
