---
ticket: {{TICKET}}
tier: {{TIER}}
plan_version: 1
base_commit: {{BASE_COMMIT}}
---

# Plan: {{TICKET}}

<!--
Frozen once approved: any change to this file cancels the approval.
State, slice status and answers that arrive later go in log.md.
IDs are machine-checked: R-nn requirements, SC-nn scenarios, S-nn slices, D-nn decisions.
Record an unknown as an open question. Never invent a contract or behaviour.
-->

## Problem and outcome

<!-- What is needed and why. What is explicitly out of scope. -->

## Requirements

<!-- Numbered acceptance criteria and constraints, one per bullet:
- R-01: A request without a customer id is rejected with 400.
-->

## Scenarios and API contract

<!-- Happy path, validation failures, authorization failures, unknown resources,
downstream errors and timeouts, duplicate requests. Every R-nn must appear in a Covers cell.
For endpoints add method, path, auth, and request/response examples in the service's real error format. -->

| ID | Scenario | Covers | Expected result |
| --- | --- | --- | --- |

## Design

<!-- Current flow with evidence (file:line), then the proposed flow.
Use a Mermaid sequence diagram only when the flow crosses more than two components. -->

### Change map

<!-- Every file to change and why. Mark uncertain locations "tentative". -->

| File | Change | Reason |
| --- | --- | --- |

## Test matrix

<!-- Written before the code. Levels: Unit, Web slice, Integration, Contract.
Every SC-nn needs a level and a named test. -->

| Scenario | Level | Test |
| --- | --- | --- |

## Slices

<!-- Ordered. Each delivers one coherent behaviour with its tests, within the
production-line cap in .github/agent-workflow.yml. Copy this block per slice:

### S-01: <goal>
- Covers: R-01, SC-01
- Paths: `src/main/java/com/example/orders/OrderService.java`, `src/test/java/com/example/orders/**`
- Tests: OrderServiceTest.rejectsMissingCustomerId
- Exit check: `./gradlew test --tests '*OrderServiceTest'` passes
-->

## Decision list

<!-- The three to five decisions only a human can make: business interpretation,
risk acceptance, commitments to other teams. The approver fills the last column. -->

| ID | Decision | Options | Recommendation | Approver's answer |
| --- | --- | --- | --- | --- |

<!-- Conditional sections. Add each one whose trigger applies; delete this comment block otherwise.

## Data and migration
(schema or stored data changes) ordering, compatibility, rollback or forward-fix

## Reliability and operations
(new outbound call or asynchronous flow) timeouts, retries, idempotency, logs and metrics

## Security and privacy
(auth behaviour or sensitive data) authorization, input validation, sensitive data, audit

## Alternatives
(required for T2) at least one real alternative and why it lost

## Open questions
(anything unresolved) Status is one of: open, answered, risk-accepted

| ID | Question | Owner | Blocking | Answer source | Status |
| --- | --- | --- | --- | --- | --- |
-->
