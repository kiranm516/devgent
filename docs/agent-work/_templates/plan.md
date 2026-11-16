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

The plan reads at three levels, each for its own reader. Keep every statement at its level:
- Functional Scenarios: what changes for a user or a caller. Read by a BA, PO or QE.
- High-Level Design: components, contracts, data and order. Read by leads and other developers.
- Low-Level Design: files, tests and slices. Read by the implementer and the reviewer.
Keep these headings and their levels exactly: hooks and CI parse them.
-->

## Functional Scenarios

<!-- Observable behaviour only, in the words of the business. No file, class or method names:
`./devgent check` rejects file references here. This layer must read correctly on its own;
`./devgent view --functional` shows it to people who never open the pull request. -->

### Problem and outcome

<!-- What is needed and why. What is explicitly out of scope. -->

### Requirements

<!-- Numbered acceptance criteria and constraints, one per bullet:
- R-01: A request without a customer id is rejected with 400.
-->

### Scenarios

<!-- Happy path, validation failures, authorization failures, unknown resources,
downstream errors and timeouts, duplicate requests. Every R-nn must appear in a Covers cell.
The expected result is what a caller observes (status, state change, message), not how. -->

| ID | Scenario | Covers | Expected result |
| --- | --- | --- | --- |

## High-Level Design

<!-- What stays true however the classes are arranged: the components involved, the contracts
between them, data, ordering and failure handling. Cite evidence (file:line) for the current
state. When nothing structural changes (T1 only), say so in one line under Proposed flow. -->

### Current flow

<!-- How it works today, with evidence (file:line). -->

### Proposed flow

<!-- How it will work. Add a Mermaid diagram when a trigger applies, and only then:
- the flow crosses more than two components: sequenceDiagram
- a status or lifecycle field gains or changes a transition: stateDiagram-v2
- steps must happen in an order across deploys (migration, backfill, flag): flowchart
- T2: the current and the proposed flow, one diagram each
`./devgent view` draws the requirement-to-test trace and the slice-to-file map
from the tables below: never draw those by hand. -->

<!-- Conditional parts of the High-Level Design. Add each one whose trigger applies;
delete this comment block otherwise.

### Contracts
(a new or changed endpoint, event or message) method, path, auth, and request/response examples
in the service's real error format

### Data and migration
(schema or stored data changes) ordering, compatibility, rollback or forward-fix

### Reliability and operations
(new outbound call or asynchronous flow) timeouts, retries, idempotency, logs and metrics

### Security and privacy
(auth behaviour or sensitive data) authorization, input validation, sensitive data, audit

### Alternatives
(required for T2) at least one real alternative and why it lost
-->

## Low-Level Design

### Change map

<!-- Every file to change and why, including the documentation the change makes stale
(README, OpenAPI description, changelog). Mark uncertain locations "tentative". -->

| File | Change | Reason |
| --- | --- | --- |

### Test matrix

<!-- Written before the code. Levels: Unit, Web slice, Integration, Contract.
Every SC-nn needs a level and a named test. -->

| Scenario | Level | Test |
| --- | --- | --- |

### Slices

<!-- Ordered. Each delivers one coherent behaviour with its tests, within the
production-line cap in .github/devgent.yml. Every SC-nn must be reached by a slice:
name it, or a requirement it covers, on a Covers line. Copy this block per slice:

#### S-01: <goal>
- Covers: R-01, SC-01
- Paths: `src/main/java/com/example/orders/OrderService.java`, `src/test/java/com/example/orders/**`
- Tests: OrderServiceTest.rejectsMissingCustomerId
- Exit check: `./gradlew test --tests '*OrderServiceTest'` passes
-->

## Decision list

<!-- The three to five decisions only a human can make: business interpretation,
risk acceptance, commitments to other teams. One block per decision; CI needs its layer,
at least two options, a recommendation and the approver's answer. The layer says who can
clarify it: functional (BA, PO, QE), high-level (a lead) or low-level (a developer).
The approver writes the answer line, or runs `./devgent answer D-01 A`. Copy this block per decision:

### D-01: <the question, answerable with one of the options>
- Layer: <functional, high-level or low-level>
- Why a human: <what makes this not the agent's call>
- A: <option and its consequence>
- B: <option and its consequence>
- Recommendation: <letter>, because <rationale with evidence, path:line>
- Reversible: <yes or no, and until when or at what cost>
- Answer:
-->

<!-- Conditional section. Add it when anything is unresolved; delete this comment block otherwise.

## Open questions
Layer is functional, high-level or low-level. Status is one of: open, answered, risk-accepted

| ID | Question | Layer | Owner | Blocking | Answer source | Status |
| --- | --- | --- | --- | --- | --- | --- |
-->
