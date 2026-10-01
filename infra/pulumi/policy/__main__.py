"""CrossGuard: `pulumi preview --policy-pack policy`; CI runs it on every pull request."""

from collections.abc import Callable
from typing import Any

import checks  # policy/checks.py: Pulumi runs this file from policy/
from pulumi_policy import (
    EnforcementLevel,
    PolicyPack,
    ReportViolation,
    ResourceValidationArgs,
    ResourceValidationPolicy,
)


def wrap(check: Callable[[str, str, dict[str, Any]], str | None]) -> Callable[..., None]:
    def validate(args: ResourceValidationArgs, report_violation: ReportViolation) -> None:
        message = check(args.resource_type, args.name, dict(args.props))
        if message:
            report_violation(message)
    return validate


PolicyPack(
    name="ipo-guardrails",
    enforcement_level=EnforcementLevel.MANDATORY,
    policies=[ResourceValidationPolicy(name=name, description=description, validate=wrap(check))
              for name, (check, description) in checks.ALL.items()],
)
