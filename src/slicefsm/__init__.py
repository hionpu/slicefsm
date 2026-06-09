"""slicefsm: a deterministic, hook-enforced slice harness.

The workflow is a finite state machine. Hooks gate which tools run in each
state and inject only the current state's prompt. MCP tools and the out-of-band
`harness` CLI are the only writers of state. See DESIGN for the full model.
"""

__version__ = "0.1.0"
