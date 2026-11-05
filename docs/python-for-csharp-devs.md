# Python for a C# developer: the parts FlowPilot uses

You already know the ideas. This page maps them. Read it next to the code.

| C# / .NET | Python (in this repo) | Where |
|---|---|---|
| `.csproj` + NuGet | `pyproject.toml` + `uv` (`uv add httpx2`, `uv sync`) | `pyproject.toml` |
| `dotnet test` | `uv run pytest` | `tests/` |
| `namespace` / folder | package = folder with `__init__.py` | `src/flowpilot/` |
| `using X;` | `from flowpilot.garageflow import GarageFlowClient` | everywhere |
| `record` | `@dataclass(frozen=True)` or pydantic `BaseModel` with `frozen=True` | `config.py`, `models.py` |
| `System.Text.Json` + DTOs | pydantic models: `Model.model_validate(json)` | `models.py` |
| `[JsonPropertyName]` / camelCase policy | `alias_generator=to_camel` | `models.py` |
| `enum` stored as text | `class Skill(StrEnum)` | `models.py` |
| `List<T>`, `Dictionary<K,V>` | `list[T]`, `dict[K, V]` | |
| `T?` (nullable) | `T \| None` | |
| Generic class `Page<T>` | `class Page(GfModel, Generic[T])` | `models.py` |
| `async Task<T>` / `await` | `async def ... -> T` / `await` | `client.py` |
| `HttpClient` | `httpx2.AsyncClient` | `client.py` |
| `using var x = ...` / `IAsyncDisposable` | `async with GarageFlowClient(...) as gf:` (`__aenter__`/`__aexit__`) | `client.py` |
| `SemaphoreSlim(1,1)` | `anyio.Lock()` | `client.py` (`_auth_lock`) |
| `Task.Delay` | `await anyio.sleep(...)` | `client.py` |
| custom exception hierarchy | `class GarageFlowNotFound(GarageFlowError)` | `errors.py` |
| `throw new X(...) ` inside `catch` | `raise X(...) from exc` (keeps the inner exception) | `client.py` |
| attributes (`[Authorize]`) | decorators (`@server.tool(...)`, `@tool_errors`) | `server.py` |
| `IOptions<T>` / user secrets | env variables + `.env` via `python-dotenv` | `config.py` |
| xUnit `[Fact]` / `[Theory]` | `def test_...()` / `@pytest.mark.parametrize` | `tests/` |
| xUnit fixtures / `IClassFixture` | `@pytest.fixture` (injected by parameter name) | `tests/conftest.py` |
| `WebApplicationFactory<Program>` | `httpx2.ASGITransport(app=sandbox)` | `tests/conftest.py` |
| Moq | small fakes (`SleepRecorder`, `MockTransport`) | `tests/` |
| `ILogger<T>` | `logging.getLogger("flowpilot.mcp")` | everywhere |
| ASP.NET Core minimal API | Starlette (FastAPI sits on top of it) | `sandbox/app.py` |
| `interface IVectorStore` | `class VectorStore(Protocol)`: any class with those methods fits ("duck typing", checked by pyright) | `knowledge/stores.py` |
| factory keyed by name | a dict of classes filled by a `@register("name")` decorator | `migration/connectors.py` |
| `switch` expression | `match provider: case "hash": ...` | `knowledge/embeddings.py` |
| LINQ `GroupBy` / `OrderBy` | dict of groups + `sorted(key=...)`, or list comprehensions | `logs/store.py` |
| `double[]` maths | `numpy` arrays: `matrix @ vector` is a matrix product | `knowledge/stores.py` |
| `File.Replace` (atomic) | write a temp file, then `os.replace` | `migration/applier.py` |

## Five Python habits that will look odd at first

1. **Indentation is the block.** No braces. 4 spaces.
2. **`self` is explicit.** Every instance method's first parameter is the instance.
3. **Keyword arguments are normal.** `gf.list_appointments(status=..., page=2)`. A bare `*` in a
   signature (`def f(self, *, page=1)`) makes everything after it keyword-only, which prevents
   "which int was which" bugs.
4. **Type hints are not enforced by Python itself.** pydantic and the MCP SDK *read* them to
   validate input and build JSON Schemas. That is why the tool signatures matter so much here.
5. **`_name` means private by convention.** Nothing stops you; the underscore says "do not".
