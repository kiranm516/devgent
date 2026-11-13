---
name: spring-boot-testing
description: How to choose test levels and write tests in a Java / Spring Boot service - unit, web slice, integration, contract, and mutation testing. Use when writing a plan's test matrix, implementing a slice's tests, or reviewing test quality.
---

# Spring Boot testing

Pick the smallest set of tests that proves the risk. Each scenario in the plan maps to one level
and one named test before any code is written.

| Level | Proves | Approach |
| --- | --- | --- |
| Unit | Domain and service rules, edge cases | JUnit 5, AssertJ, Mockito where useful. No Spring context. |
| Web slice | HTTP mapping, validation, status codes, error bodies | `@WebMvcTest` with MockMvc; mock the service |
| Integration | Wiring, persistence, transactions, serialization | `@SpringBootTest`; Testcontainers for the real database or broker |
| Contract | Assumptions about another service | OpenAPI or schema validation; consumer and provider contract tests where they exist |

## Choosing a level

- A business rule: unit test. Do not start a Spring context to test an `if`.
- A status code, validation message or error body: web slice.
- A query, transaction boundary, migration or JSON mapping of a real type: integration.
- Anything you believe about another service: contract. A mocked downstream call proves your code
  handles the response you imagined; it does not prove the real contract is compatible.

## Writing them

- Name tests after the behaviour: `rejectsOrderWithoutCustomerId`, not `testCreate2`.
- Assertions come from the plan's scenarios. Do not derive expected values by running the
  implementation and pasting what it returned.
- One behaviour per test. Assert the outcome (status, body, stored state, published event), not
  that a mock was called, unless the call is the behaviour.
- Cover the failure paths in the scenario table, not only the happy path.
- No `Thread.sleep`; use Awaitility for asynchronous flows. No dependence on test order, wall
  clock (inject a `Clock`) or shared mutable state.
- Do not use an in-memory database to prove behaviour of a different production database.
- Test data is synthetic. Never production data, real credentials or real customer identifiers.

## Running

Use the repository's own commands (see `.github/copilot-instructions.md`), for example
`./gradlew test --tests '*OrderServiceTest'` for one class and `./gradlew check` for the full
verification. Report the exact command and its real result.

## Test quality

Line coverage is a diagnostic only. Run mutation testing (PIT, `./gradlew pitest` where
configured) on the classes a slice changed: a surviving mutant in changed code is a test that
would not notice the behaviour breaking. Tests ship in the same slice as the code they prove and
are reviewed, but do not count toward the slice's line cap.

The existing build pipeline is the authority for compilation, tests, formatting, static analysis
and secret scanning. Run and explain those checks; never replace or weaken them.
