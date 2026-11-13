---
name: spring-rest-api-design
description: Checklist for designing or changing a REST endpoint in a Java / Spring Boot service - contract, status codes, validation, error bodies, idempotency, compatibility. Use when writing the "Scenarios and API contract" or "Design" sections of a plan, or when implementing a controller.
---

# Spring REST API design

Find the service's existing conventions first and copy them. Cite the file you copied from. If the
service has an OpenAPI file, it is the contract: change it in the same slice as the controller.

## Contract

- Resource-oriented paths, plural nouns, no verbs: `POST /orders`, `GET /orders/{id}`.
- State the method, path, auth requirement, request body, response body and every status code.
- Use the service's real error format in examples. If none exists, propose RFC 9457
  `application/problem+json` (Spring's `ProblemDetail`) as a decision for the approver.
- Request and response types are dedicated DTOs (records). Never expose JPA entities.

## Status codes

| Situation | Status |
| --- | --- |
| Created | 201 with `Location` |
| Read, update with body | 200 |
| Success, no body | 204 |
| Malformed or invalid input | 400 |
| Not authenticated / not allowed | 401 / 403 |
| Unknown resource | 404 |
| State conflict, duplicate | 409 |
| Downstream failure / timeout | 502 / 504 (never leak the downstream body) |

## Scenarios every endpoint needs in the plan

Happy path; each validation failure; authorization failure (no token, wrong role, another
tenant's resource); unknown resource; downstream error and timeout; duplicate request.

## Implementation

- Validate at the edge: `@Valid` on the body, Bean Validation on DTOs, constraints on path and
  query parameters. Business rules live in the service layer, not the controller.
- Map exceptions in one `@RestControllerAdvice`; controllers do not catch and translate.
- Controllers stay thin: map, delegate, map back. Layering is controller -> service -> repository
  or client; never skip a layer or call back up.
- `POST` that creates or triggers a side effect: decide idempotency explicitly (an
  `Idempotency-Key` header or a natural key). Retrying a downstream call without confirmed
  idempotency is a decision-list item, not an implementation detail.
- Collections are paginated with a bounded page size.
- Outbound calls have explicit connect and read timeouts. Adding one makes the ticket T2 and
  requires the "Reliability and operations" section.

## Compatibility

Compatible: adding an optional request field, a response field, an endpoint. Breaking: removing or
renaming anything, changing a type or status code, making an optional field required, tightening
validation. A breaking change is T2 and needs the owner of every known consumer in the decision list.
Never guess another team's contract: record it as an open question with an owner.
