---
name: minimal-change
description: How to solve a requirement with the least new code - reuse what the codebase, the platform and the installed dependencies already give you, and never cut validation, error handling or security. Use when planning a change map and slices, implementing a slice, and reviewing a diff for code no requirement needs.
---

# Minimal change

The best code is the code never written. Solve the whole requirement with the least new code.
The diff ends up small because every line is needed, not because it was compressed.

## Before you write

Read the requirement and the code it touches. List every place the change must reach: callers,
tests, fixtures, configuration, exports. Check what it could break: data it would destroy or
expose, callers that stop working. That is the scope. Extra features are not.

## The ladder

Take the first option that fully works:

1. **Does it need to exist?** Skip features, options and flexibility no requirement asks for.
2. **Already in this codebase?** A helper, component, service or pattern: use it the way the
   surrounding code does.
3. **Standard library or platform feature?** Use it, unless the project has its own.
4. **An installed dependency?** Use it. A new dependency is never the answer to a few lines, and
   in this workflow it is a plan mismatch.
5. **One line a reader gets at a glance?** One line. A line that needs decoding is not short.
6. **Otherwise:** the minimum code that works.

## Rules

- Be lazy about the solution, never about the change: finish everything the slice needs.
- No abstraction, wrapper, type conversion, option, configuration or "for later" code nobody asked
  for. Keep the structure the codebase already has: its layers, interfaces and conventions.
- Deletion beats addition.
- Bug fix: search every caller of the function you touch, then fix the root cause once in the
  shared code.
- Code you move or merge keeps its error handling and validation.
- Between options of equal size, take the one that is correct on edge cases.
- Comment only the why the code cannot show, in one line.
- A shortcut with a known limit gets a comment: `shortcut: <the limit>, <when to upgrade>`.

## Never cut

- Validation at trust boundaries
- Error handling that prevents data loss
- Security
- Anything a requirement in the plan asks for, and the tests in its test matrix

## The plan bounds the change

- The architect lists every caller, test and fixture a change breaks in the change map, so each
  one lands in a slice.
- The implementer writes only the active slice's paths. If the change breaks something outside
  them, stop and log a `mismatch`; do not edit the file. On a T0 ticket the task note's `Files`
  list is the boundary: stop and tell the driver.
- The reviewer reads the callers of changed code, not only the diff. Code that no requirement
  needs goes in the review brief under "Deviations from the plan", naming the requirement gap or
  the existing helper it duplicates. It is not a finding unless it has a concrete failure scenario.
