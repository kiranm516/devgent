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
Add a Mermaid diagram when a trigger applies, and only then:
- the flow crosses more than two components: sequenceDiagram
- a status or lifecycle field gains or changes a transition: stateDiagram-v2
- steps must happen in an order across deploys (migration, backfill, flag): flowchart
- T2: the current and the proposed flow, one diagram each
`./agentwf view` draws the requirement-to-test trace and the slice-to-file map
from the tables below: never draw those by hand. -->

### Change map

<!-- Every file to change and why, including the documentation the change makes stale
(README, OpenAPI description, changelog). Mark uncertain locations "tentative". -->

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
risk acceptance, commitments to other teams. One block per decision; CI needs at
least two options, a recommendation and the approver's answer. The approver writes
the answer line, or runs `./agentwf answer D-01 A`. Copy this block per decision:

### D-01: <the question, answerable with one of the options>
- Why a human: <what makes this not the agent's call>
- A: <option and its consequence>
- B: <option and its consequence>
- Recommendation: <letter>, because <rationale with evidence, path:line>
- Reversible: <yes or no, and until when or at what cost>
- Answer:
-->

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
