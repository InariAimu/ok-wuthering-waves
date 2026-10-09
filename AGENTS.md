# Agent Instructions

## Python

- When running Python commands in this repository, use the local virtual environment if it exists.
- On Windows/PowerShell, prefer `.\.venv\Scripts\python.exe` when present.
- On POSIX shells, prefer `./.venv/bin/python` when present.
- Fall back to `python` only when no local `.venv` interpreter exists.
- Prefer invoking the interpreter directly, for example `.\.venv\Scripts\python.exe -m pytest`, instead of relying on shell activation.

## Echo enhancement

- For inventory inspection, model enhancement, campaign recovery, or equipment verification, read `docs/echo-enhancement-agent-guide.md` first.
- The current adaptive policy and log schema are described in `docs/echo-adaptive-enhancement.md`; older campaign scripts and handoff notes include historical behavior.
