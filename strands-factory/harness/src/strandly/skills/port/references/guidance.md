# Translation Guidance

Cross-language rules and mappings for porting inside the Strands SDK monorepo. Each package's
`docs/PORTING.md` and `docs/TESTING.md` are authoritative for its own conventions; append new
mappings and gotchas here (1–3 lines each) when a port surfaces them.

## Universal rules

1. **Translate, don't improve.** Reproduce the source behavior exactly. Don't fix bugs, add error
   handling the source lacks, refactor adjacent code, or redesign the feature.

2. **Idiomatic over literal, when confident.** Prefer target-language idioms and patterns. When
   you're uncertain whether an idiom changes behavior, fall back to a literal translation and flag
   it in the decision log.

3. **The source tests are the behavioral spec.** Each source test — unit and integration — asserts a
   behavior; every one must have a corresponding target test, or it is a `missing-behavior` finding.
   The tests define what "correct" means, not the implementation structure. Read `docs/TESTING.md`
   for test layout and execution (see [Discovering project conventions](#discovering-project-conventions)).

4. **One feature, nothing else.** Do not touch code outside the feature scope. If you discover
   pre-existing issues in the target codebase, flag them — don't fix them.

5. **Consistency over precedent when precedents conflict.** If the target codebase uses two patterns
   for the same thing (e.g., both dataclasses and TypedDicts for config objects), check which is
   the current standard. If unclear, flag it and ask rather than guessing.

6. **Name the gap, don't hide it.** When the source uses something that has no equivalent in the
   target (a stdlib function, a language feature, a library), explicitly surface it as a language
   gap with a proposed resolution. Never silently substitute.

7. **Design for portability.** Clean, well-structured implementations translate more
   straightforwardly. If the source is tangled, that's a signal it needs cleanup before translation
   — flag it rather than working around it.

---

## TypeScript → Python

### Construct mapping

| TypeScript | Python | Notes |
|---|---|---|
| `interface` (passed as constructor/function param) | `TypedDict` | NOT dataclass. TypedDicts support `**kwargs` spreading into constructors. |
| `interface` (with methods or used as a contract) | `Protocol` | Use Protocol for structural subtyping (duck typing). |
| `class` | `class` | Direct mapping. |
| `enum` | `enum.Enum` or `StrEnum` | Use `StrEnum` for string enums (Python 3.11+). |
| `type` alias | `TypeAlias` or `type` statement | Use `type X = ...` (Python 3.12+) or `X: TypeAlias = ...`. |
| `T extends Base` (generic constraint) | `TypeVar("T", bound=Base)` | Or use the Python 3.12 `def f[T: Base]()` syntax if the project uses it. |
| optional param `param?: Type` | `param: Type \| None = None` | |
| `Record<string, T>` | `dict[str, T]` | |
| `Promise<T>` | `Awaitable[T]` or `async def -> T` | Match the target's async pattern. |
| `readonly` property | `@property` (no setter) | Or a frozen dataclass field if applicable. |
| `namespace` | Module (separate `.py` file) | Python has no namespace construct. |
| `import { X } from "./module"` | `from .module import X` | Relative imports for intra-package. |

### Testing patterns

| TypeScript | Python |
|---|---|
| Jest / Vitest | pytest |
| `describe` / `it` blocks | Test functions or classes (`test_` prefix) |
| `beforeEach` / `afterEach` | `@pytest.fixture` (with yield for teardown) |
| `jest.mock()` / `vi.mock()` | `unittest.mock.patch` or `pytest-mock` (`mocker` fixture) |
| `expect(x).toBe(y)` | `assert x == y` |
| `expect(fn).toThrow()` | `with pytest.raises(ExceptionType):` |
| `expect(fn).toHaveBeenCalledWith(...)` | `mock.assert_called_with(...)` |
| Test file: `module.test.ts` | Test file: `test_module.py` (in a `tests/` directory mirroring `src/`) |

### Known gotchas (from past translations)

- **TypedDict vs dataclass confusion.** The Python codebase historically uses both for similar
  patterns (e.g., `CacheConfig` is a dataclass, `BaseModelConfig` is a TypedDict). The rule: a TS
  `interface` passed as an object literal to a constructor → TypedDict. A TS `class` with behavior
  → Python class. If the target has conflicting precedents, flag it.

- **Protocol used where TypedDict was needed.** A prior port used Protocol for a config type,
  which broke `**kwargs` spreading. Protocols define method contracts; TypedDicts define data
  shapes. Don't confuse them.

- **No uuid v7 in Python < 3.14.** The TS SDK uses uuid v7 for ordered document IDs. Python's
  `uuid` module has no v7 until 3.14. This is a genuine language gap — flag it with options:
  (a) use uuid4 (loses ordering), (b) add a third-party dep like `uuid7`, (c) implement the
  algorithm inline.

- **boto3-stubs side effects.** Adding `boto3-stubs[service]` installs overloads globally via
  package metadata, not per-import. This can surface pre-existing type errors in unrelated files.
  If adding stubs would break other code, flag it rather than fixing the world.

- **Python packaging: `__init__.py` re-exports.** TS uses barrel files (`index.ts`). Python
  equivalently uses `__init__.py` to re-export public names. Check whether the target package
  does this — some packages use explicit imports from submodules instead.

---

## Python → TypeScript

### Construct mapping

| Python | TypeScript | Notes |
|---|---|---|
| `TypedDict` | `interface` | Direct mapping for data shapes. |
| `Protocol` | `interface` (with methods) | TS interfaces are structurally typed like Protocols. |
| `dataclass` | `class` or `interface` + factory | If the dataclass has no methods, an interface + object literal may be more idiomatic. |
| `class` | `class` | Direct mapping. |
| `Enum` / `StrEnum` | `enum` or string union | Prefer `const enum` or string union types for simple cases. |
| `TypeAlias` / `type` statement | `type X = ...` | Direct mapping. |
| `dict[str, T]` | `Record<string, T>` | |
| `T \| None` | `T \| undefined` or `T?` | Use optional param `?` for function args; use `\| undefined` for type positions. |
| `@property` | getter/setter or `readonly` | |
| `async def` | `async function` returning `Promise<T>` | |
| `with` (context manager) | `using` (if available) or try/finally | |
| `pytest.fixture` | `beforeEach` / helper function | TS has no fixture injection; use setup functions. |
| `unittest.mock.patch` | `jest.mock()` / `vi.mock()` | |

### Known gotchas

- **Python's `None` vs TS's `undefined` vs `null`.** Python uses `None` for absence. TS
  distinguishes `undefined` (not set) from `null` (explicitly empty). Default to `undefined`
  unless the source explicitly handles null semantics.

- **Decorator patterns.** Python decorators that return wrapped functions (like `@tool`) may map
  to TS decorator syntax, higher-order functions, or class-based patterns depending on the
  target's conventions. Check what the TS SDK uses for the equivalent pattern.

- **Default mutable arguments.** Python's mutable default gotcha (`def f(x=[])`) doesn't exist in
  TS, but if the source carefully avoids it (using `None` + conditional), don't drop that logic —
  it may be intentional for a reason beyond the Python footgun.

