---
name: secure-api-review
description: Security review pass for a Java / Spring Boot REST API change - authorization, input validation, injection, sensitive data, secrets, outbound calls, dependencies. Use for the reviewer's security pass on any diff, for every T2 ticket, and when writing a plan's "Security and privacy" section.
---

# Secure API review

Report only what you can tie to a concrete failure: who sends what, and what they get that they
should not. Give file and line. Anything else is a question.

## Authentication and authorization

- Every new or changed endpoint has an explicit rule (`SecurityFilterChain` matcher or
  `@PreAuthorize`). A new path must not fall through to a permissive default.
- Object-level checks: can user A read or change user B's resource by changing an id? Check the
  query or service enforces ownership or tenancy, not only the role.
- Function-level checks: admin-only operations are not reachable with a normal role.
- A test proves the 401 and the 403, not just the 200.
- Changes to `**/security/**`, filters, CORS, CSRF or session settings are T2.

## Input

- Bean Validation on every request DTO, with bounds on sizes, lengths and page sizes.
- No mass assignment: request DTOs do not carry fields the caller must not set (`id`, `role`,
  `ownerId`, `status`).
- No string-built queries: JPQL and SQL use bind parameters; `@Query` with concatenation and
  `nativeQuery` with user input are findings.
- No user-controlled values in file paths, redirects, outbound URLs (SSRF), SpEL, or
  deserialization of arbitrary types.

## Output and data

- Responses use DTOs; entities, stack traces, SQL errors and downstream bodies never reach the client.
- Sensitive fields (credentials, tokens, personal data, payment data) are not logged, not put in
  exception messages, not returned unless the contract requires it, and not in metrics labels.
- Changes that store or expose sensitive data need the plan's "Security and privacy" section and
  an audit decision.

## Secrets and configuration

- No credentials, tokens or keys in code, tests, configuration files or the ticket artifacts.
- Nothing weakened in `application*.yml`: actuator exposure, TLS verification, debug logging,
  permissive CORS.

## Outbound calls and dependencies

- A new outbound host is T2. It has timeouts, does not forward the caller's credentials unless
  designed to, and validates what comes back.
- Retries are safe only with confirmed idempotency.
- A new or upgraded dependency is in the plan, comes from the organisation's approved source, and
  passes the existing dependency scan.

## What this pass is not

Secret scanning, dependency scanning and static analysis belong to CI. Do not repeat them; read
their results and report only failures. Never test against production endpoints, and never put
real credentials or customer data into a prompt, a test or a finding.
