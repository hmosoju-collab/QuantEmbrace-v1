# QuantEmbrace - Definition of Done

> A change is not "done" until every applicable criterion below is satisfied.
> Reviewers must verify each item before approving a pull request.

---

## 1. Code Changes

A code change is done when **all** of the following are true:

### Type Safety
- [ ] All function signatures include complete type hints (parameters and return types).
- [ ] No use of `Any` type unless explicitly justified with a comment explaining why.
- [ ] Pydantic models are used for all data crossing service boundaries.
- [ ] `mypy --strict` passes with zero errors for the changed files.

### Documentation
- [ ] Every public function and class has a docstring following Google-style format.
- [ ] Docstrings include parameter descriptions, return value descriptions, and exception descriptions.
- [ ] Complex algorithms include inline comments explaining the "why", not the "what".
- [ ] Module-level docstring present for new files describing the module's purpose.

```python
# Example of expected documentation quality
def validate_position_limit(
    signal: Signal,
    current_positions: dict[str, Position],
    risk_params: RiskParameters,
) -> ValidationResult:
    """Validate that a signal would not breach position limits.

    Checks the proposed signal against current portfolio positions and
    configured risk parameters. Both per-instrument and portfolio-level
    limits are evaluated.

    Args:
        signal: The trading signal to validate.
        current_positions: Map of instrument symbol to current Position.
        risk_params: Active risk parameters for the strategy.

    Returns:
        ValidationResult with approved=True if within limits, or
        approved=False with a rejection reason.

    Raises:
        RiskEngineUnavailableError: If risk parameter lookup fails.
    """
```

### Testing
- [ ] Unit tests cover all new or changed public functions.
- [ ] Unit tests cover both happy path and error/edge cases.
- [ ] Critical paths (risk validation, order submission, kill switch) have minimum 90% line coverage.
- [ ] Non-critical paths have minimum 80% line coverage.
- [ ] Tests do not depend on network calls, real broker APIs, or wall-clock time.
- [ ] Test names clearly describe the scenario: `test_validate_order_rejects_when_daily_loss_limit_breached`.

### Separation of Concerns
- [ ] No strategy logic in the execution engine.
- [ ] No execution logic in the strategy engine.
- [ ] No direct broker API calls outside the execution engine's broker adapters.
- [ ] No risk validation logic outside the risk engine.
- [ ] Shared code lives in `/services/shared/`, not duplicated across services.
- [ ] No service imports code directly from another service (only shared is allowed).

### Risk Validation Hooks
- [ ] Pre-commit hooks pass (lint, type check, import boundary check).
- [ ] If the change affects order flow, the risk engine integration test suite passes.
- [ ] If the change affects risk parameters, the change has been reviewed by a second person.
- [ ] No hardcoded credentials, API keys, or secrets anywhere in the code.

### Code Quality
- [ ] `ruff` linter passes with zero warnings.
- [ ] `ruff format` produces no changes (code is already formatted).
- [ ] No TODO comments without a linked issue number.
- [ ] No commented-out code blocks.
- [ ] No print statements (use structured logging).
- [ ] All logging uses `structlog` with appropriate log levels and correlation IDs.

---

## 2. Architecture Changes

An architecture change is done when **all** of the following are true:

### Pre-Implementation Documentation
- [ ] Architecture document updated in `/architecture/` **before** code changes begin.
- [ ] The change is documented with context (what problem it solves, alternatives considered, why this approach).
- [ ] Data flow diagrams updated if data paths change.
- [ ] Service interaction diagrams updated if service boundaries change.
- [ ] The decision is recorded in `/memory/decisions.md` with rationale.

### Impact Analysis
- [ ] All affected services are identified and their owners notified.
- [ ] Breaking changes to inter-service contracts are documented with a migration path.
- [ ] Performance impact is estimated (latency, throughput, resource usage).
- [ ] Failure modes are documented (what happens when this component fails).

### Review
- [ ] Architecture change has been reviewed by at least two contributors.
- [ ] If the change adds a new service, it is registered in `/governance/file_structure.md`.
- [ ] If the change adds new infrastructure, Terraform modules are updated.
- [ ] If the change affects risk boundaries, the risk engine maintainer has explicitly approved.

---

## 3. New Trading Strategies

A new strategy is done when **all** of the following are true:

### Backtesting
- [ ] Backtest has been run against a minimum of 2 years of historical data (where available).
- [ ] Backtest results document is generated with the following metrics:
  - Total return and annualized return
  - Sharpe ratio and Sortino ratio
  - Maximum drawdown (peak-to-trough)
  - Win rate and profit factor
  - Average trade duration
  - Number of trades
  - Turnover rate
- [ ] Backtest results include transaction cost assumptions (brokerage, slippage, STT/taxes for NSE).
- [ ] Strategy has been tested across different market regimes (trending, ranging, volatile, low-volatility).
- [ ] Out-of-sample testing performed (train on one period, validate on another).

### Risk Parameters
- [ ] Risk parameters are defined and documented for the strategy:
  - Maximum position size (absolute and as % of portfolio)
  - Maximum number of concurrent positions
  - Stop-loss levels
  - Maximum daily loss for this strategy
  - Maximum drawdown before strategy is paused
  - Allowed instruments and exchanges
- [ ] Risk parameters are stored in DynamoDB, not hardcoded.
- [ ] Kill switch integration is verified (strategy halts when kill switch is activated).

### Code Quality
- [ ] Strategy implements the `BaseStrategy` interface completely.
- [ ] Strategy emits `Signal` objects only (no direct order creation).
- [ ] Strategy does not import from execution engine or broker adapters.
- [ ] Unit tests cover signal generation logic.
- [ ] Integration test verifies the strategy works in the signal-to-risk-to-execution pipeline.

### Deployment
- [ ] Strategy is configurable to run in paper-trading mode.
- [ ] Strategy has been run in paper-trading mode for a minimum of 5 trading days before live deployment.
- [ ] Paper trading results are reviewed and compared against backtest expectations.

---

## 4. Infrastructure Changes

An infrastructure change is done when **all** of the following are true:

### Cost Impact
- [ ] Cost impact is documented (monthly estimated cost delta).
- [ ] If monthly cost increases by more than $50, the change requires explicit approval.
- [ ] Cost optimization alternatives have been considered and documented.

### Terraform
- [ ] `terraform plan` output is included in the pull request description.
- [ ] No unexpected resource deletions or replacements in the plan.
- [ ] `terraform validate` passes.
- [ ] `tflint` passes with zero warnings.
- [ ] All new resources follow naming conventions from `/governance/naming_conventions.md`.
- [ ] Sensitive values use `sensitive = true` in Terraform outputs.
- [ ] State is managed in the shared S3 backend with DynamoDB locking.

### Security
- [ ] IAM roles follow least-privilege principle.
- [ ] No wildcard (`*`) permissions on IAM policies unless specifically justified.
- [ ] Security groups follow least-privilege (no 0.0.0.0/0 ingress unless public-facing).
- [ ] Secrets are stored in Secrets Manager, not in environment variables or SSM parameters.
- [ ] Encryption at rest is enabled for all new storage resources (S3, DynamoDB, SQS).

### Review
- [ ] Infrastructure change has been reviewed by at least one person familiar with the AWS account.
- [ ] Changes to networking (VPC, subnets, security groups) require two approvals.

---

## 5. Deployments

A deployment is done when **all** of the following are true:

### Pre-Deployment
- [ ] All code changes meet the "Code Changes" definition of done above.
- [ ] All tests pass in CI (unit, integration, and relevant backtest tests).
- [ ] Docker image builds successfully and is pushed to ECR.
- [ ] Staging environment deployment is completed and smoke-tested.
- [ ] Staging smoke tests include:
  - Service starts without errors
  - Health check endpoint responds
  - Service can connect to its dependencies (DynamoDB, S3, SQS)
  - For trading services: paper trade flow completes end-to-end

### Monitoring
- [ ] CloudWatch alarms are configured for the service:
  - ECS task health (running count, restart count)
  - Error rate (5xx responses, unhandled exceptions)
  - Latency (p50, p95, p99 for critical paths)
  - Business metrics where applicable (signal rate, order fill rate)
- [ ] Log group is configured with appropriate retention period.
- [ ] Dashboard is updated to include the new or changed service metrics.
- [ ] Alert routing is configured (SNS topic to appropriate notification channel).

### Rollback Plan
- [ ] Rollback procedure is documented (which ECS task definition revision to roll back to).
- [ ] Rollback has been tested at least once in staging.
- [ ] Database migration rollback procedure is documented (if applicable).
- [ ] Feature flags are in place for gradual rollout (if applicable).

### Post-Deployment
- [ ] Production deployment is monitored for a minimum of 30 minutes after deployment.
- [ ] No new errors or anomalies observed in logs and metrics.
- [ ] Trading behavior is verified against expected patterns (for trading services).
- [ ] Deployment is recorded in the deployment log with version, timestamp, and deployer.

---

## Quick Reference Checklist

For PR reviewers, verify the applicable sections:

| Change Type | Sections to Verify |
|---|---|
| Bug fix in existing service | 1 (Code Changes) |
| New feature in existing service | 1 (Code Changes), possibly 2 (Architecture) |
| New trading strategy | 1 (Code Changes), 3 (New Strategies) |
| Terraform changes | 4 (Infrastructure Changes) |
| Production deployment | 5 (Deployments) |
| New service | 1 + 2 + 4 + 5 (all sections) |
