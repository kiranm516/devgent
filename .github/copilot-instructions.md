# Repository conventions

<!-- Fill the TODO values when this kit is installed in a service repository. Keep this file
short: procedures belong in .github/skills, checks belong in scripts. -->

## Stack

- Java TODO (e.g. 21), Spring Boot TODO (e.g. 3.x), Gradle wrapper (`./gradlew`).
- Database TODO; migrations with TODO (Flyway: `src/main/resources/db/migration`).

## Commands

- Full verification: `./gradlew check`
- One test class: `./gradlew test --tests '*OrderServiceTest'`
- Workflow gates, as CI runs them: `./agentwf check`

The build pipeline is the authority for compilation, tests, formatting, static analysis and secret
scanning. Run it and report its real output.

## Layering

- `controller` -> `service` -> `repository` / `client`. No skipping, no calls back up.
- Controllers map and delegate; business rules live in services; entities never leave the service layer.
- Request and response types are DTO records.
- Outbound HTTP lives in `client` packages, with explicit timeouts.

## Error handling

- One `@RestControllerAdvice` maps exceptions to the service's error format: TODO (cite the class).
- Never return stack traces, SQL errors or downstream response bodies to callers.

## Tests

- JUnit 5 and AssertJ. Levels and rules: the `spring-boot-testing` skill.
- Tests ship in the same commit as the code they prove.

## Commits

- One commit per slice, with the trailer `Slice: S-nn`.
- No unrelated formatting, dependency upgrades or refactoring in a slice.
