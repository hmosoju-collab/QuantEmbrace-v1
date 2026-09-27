"""Cost controls: a per-run token budget and a consecutive-failure breaker."""


class BudgetExhausted(RuntimeError):
    pass


class RunBudget:
    """Reserves the worst case (``max_tokens``) before each call so a run can
    never overshoot its budget, then books the actual usage."""

    def __init__(self, max_run_tokens: int):
        self.max_run_tokens = max_run_tokens
        self.used = 0

    def reserve(self, max_tokens: int) -> None:
        if self.used + max_tokens > self.max_run_tokens:
            raise BudgetExhausted(
                f"token budget exhausted ({self.used}/{self.max_run_tokens} used, "
                f"call needs up to {max_tokens})"
            )

    def record(self, input_tokens: int, output_tokens: int) -> None:
        self.used += input_tokens + output_tokens


class CircuitBreaker:
    """Opens after ``threshold`` consecutive provider failures and stays open
    for the rest of the run: every remaining component reports UNAVAILABLE
    instead of hammering a failing endpoint."""

    def __init__(self, threshold: int):
        self.threshold = threshold
        self.consecutive_failures = 0

    @property
    def is_open(self) -> bool:
        return self.consecutive_failures >= self.threshold

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        self.consecutive_failures += 1
