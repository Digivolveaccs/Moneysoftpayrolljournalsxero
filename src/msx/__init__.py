"""msx - Moneysoft Payroll Manager -> Xero payroll journal pipeline (Digivolve).

Deterministic, stdlib-only core. Every module either returns a fully proven
result or raises ``msx.errors.Hold`` with a human-actionable message. Nothing
in this package ever adjusts a figure to make something balance.
"""

__version__ = "0.1.0"
