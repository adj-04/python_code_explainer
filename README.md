# Pythonic — Python Code Explainer

A small dockerized web app for understanding Python code — combining an AI explainer with a fully local static-analysis toolkit (flowcharts, code-smell detection, complexity/Big-O estimates, and optimization tips).

Paste a snippet of Python code into the browser and:
- Hit **Explain it** for a step-by-step, beginner-friendly AI explanation (uses the Groq API).
- Hit **Flowchart**, **Find issues**, **Complexity**, or **Optimize** for instant results computed entirely with Python's built-in `ast` module — no API calls, no quota used, works even with `USE_GROQ=false`.

## How it works

- **Frontend** — a black-and-yellow static page (served by nginx) with a code box, an AI "Explain it" button, and four local-analysis buttons.
- **Backend** — a Flask API. `POST /explain` validates input locally then calls Groq. `POST /flowchart`, `/explain-local`, `/issues`, `/complexity`, `/optimize`, and `/analyze` run purely on `backend/analyzer.py`, an offline AST-based engine — they never touch Groq or the daily quota.
- **Groq** — runs open models (Llama 3.3 70B by default) on custom LPU hardware; used only for the one AI-powered endpoint.

```
                    ┌──> /explain              ──> Groq API (uses quota)
Browser  --> Flask ─┤
                    └──> /flowchart /issues /complexity /optimize /analyze
                              └──> analyzer.py (ast module, 100% local, free)
```

## Project structure

```
pce-improved/
├── backend/
│   ├── app.py            # Flask API (routes + Groq call)
│   ├── analyzer.py       # Offline AST-based analysis engine (no API calls)
│   ├── requirements.txt  # Python dependencies
│   ├── dockerfile
│   └── .env.example      # copy to .env and add your key
├── frontend/
│   ├── index.html        # UI
│   └── dockerfile
└── docker-compose.yml
```

## Prerequisites

- Docker and Docker Compose installed (`docker compose` or the older `docker-compose`)
- A free Groq API key from [console.groq.com/keys](https://console.groq.com/keys) — no credit card needed

## Setup

1. Get into the project folder:
   ```bash
   cd pce-improved
   ```

2. Create your backend `.env` file from the example:
   ```bash
   cp backend/.env.example backend/.env
   ```

3. Open `backend/.env` and paste in your Groq API key:
   ```
   USE_GROQ=true
   GROQ_API_KEY=your-real-key-here
   GROQ_MODEL=llama-3.3-70b-versatile
   ```

4. Build and start the containers:
   ```bash
   docker compose up --build
   # or, on older setups: docker-compose up --build
   ```

5. Open the app in your browser:
   - Frontend: [http://localhost:8080](http://localhost:8080)
   - Backend health check: [http://localhost:5000/health](http://localhost:5000/health)

## API reference

### `POST /explain`

**Request body:**
```json
{ "code": "def add(a, b):\n    return a + b" }
```

**Success response (200):**
```json
{ "explanation": "This function takes two arguments, a and b, and returns their sum..." }
```

**Error responses:**
| Status | Meaning |
|---|---|
| 400 | No code / empty code sent |
| 413 | Code exceeds the max length (`MAX_CODE_LENGTH` in `.env`) |
| 422 | Input isn't valid Python (rejected locally, before calling Groq) |
| 429 | Per-IP rate limit or the daily request cap was hit |
| 503 | Groq isn't enabled or the API key is missing |
| 500 | Groq API call failed |

### `GET /health`

Returns `{"status": "ok", "groq_enabled": true/false, "daily_quota_remaining": N}` — useful for uptime checks or container orchestrators.

### Local analysis endpoints (no Groq, no quota)

All of these take the same request body as `/explain` (`{"code": "..."}`) and are powered entirely by `backend/analyzer.py`. They work even when `USE_GROQ=false`.

| Endpoint | Returns |
|---|---|
| `POST /flowchart` | `{"nodes":[...], "edges":[...], "function_analyzed":"...", "truncated":bool}` — a structured control-flow graph for the first function's body (or module-level code). Rendered as SVG directly by the frontend — no external diagram library, so a diagram can never fail to parse. |
| `POST /explain-local` | `{"lines":[{"line","code","explanation","depth"}, ...], "function_analyzed":"...", "truncated":bool}` — a rule-based, line-by-line walkthrough (one row per statement, with nesting depth) for a code \| explanation split view. No LLM involved. |
| `POST /issues` | `{"issues": [{"line", "severity", "category", "message"}, ...]}` — code smells: mutable default args, bare/silent `except`, unused imports, `eval`/`exec` use, `== None` comparisons, deep nesting, overly long functions, too many parameters, builtin shadowing, `global` usage. |
| `POST /complexity` | `{"complexity": [{"name", "line", "cyclomatic_complexity", "rating", "big_o", "lines"}, ...]}` — per-function McCabe cyclomatic complexity and a heuristic Big-O guess based on loop nesting depth and recursion (flags unmemoized recursion as exponential). |
| `POST /optimize` | `{"suggestions": [{"line", "title", "detail"}, ...]}` — pattern-based tips: loop-that-only-appends → list comprehension, string `+=` in a loop → `''.join()`, `range(len(x))` → `enumerate()`, `x in a_list` in a loop → use a `set`, unmemoized recursion → `@lru_cache`. |
| `POST /analyze` | Runs all four of the above in one call and returns a combined object. |

Same 400/413/422 error codes as `/explain` apply (missing code, too long, not valid Python) — but there's no 429/503/500-from-Groq, since these never leave the container.

## Configuration

All config lives in `backend/.env`:

| Variable | Default | Description |
|---|---|---|
| `USE_GROQ` | `false` | Must be `true` to enable the Groq integration |
| `GROQ_API_KEY` | *(empty)* | Your Groq API key |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Which Groq model to call |
| `ALLOWED_ORIGINS` | `*` | Comma-separated CORS origins, or `*` for any |
| `MAX_CODE_LENGTH` | `8000` | Max characters accepted per request |
| `RATE_LIMIT` | `10 per hour` | Per-IP limit on `/explain` calls |
| `MAX_REQUESTS_PER_DAY` | `50` | Hard cap on total `/explain` calls per day, across all users |
| `PORT` | `5000` | Backend port |

## Protecting your Groq quota

Two independent layers guard against burning through your API quota once this is deployed publicly:

1. **Local Python validation (free)** — every request is checked with Python's own `ast.parse()` *before* it's sent to Groq. Prose, HTML, SQL, and most other non-Python input gets rejected with a 422 and never touches the API, at zero extra cost. **Known limitation:** a few lines of JavaScript-like code (e.g. `console.log("hi")`) happen to be valid Python syntax too (it parses as a method call), so it can slip through. This isn't a perfect language classifier — that would require its own model call, which defeats the point of doing it for free — but it blocks the overwhelming majority of non-Python input.
2. **Rate limiting + daily cap** — `RATE_LIMIT` throttles how often a single IP can call `/explain`, and `MAX_REQUESTS_PER_DAY` is a hard ceiling on total calls per day across everyone, reset at midnight server time. Once it's hit, the app returns a 429 instead of calling Groq, so you can never accidentally exceed your plan.

Groq's free tier is roughly 30 requests/minute and up to a few thousand requests/day depending on the model — check current numbers on your [Groq console](https://console.groq.com/) dashboard, since limits vary by model and change over time.

3. **Most of the app doesn't call Groq at all.** Flowchart, Find issues, Complexity, and Optimize are pure `ast` analysis — you can demo, develop, and load-test the entire app with `USE_GROQ=false` and never spend a token. Only flip `USE_GROQ=true` when you actually want the AI explanation feature live.

## Deploying

- The backend runs on **gunicorn** instead of the Flask dev server, so it's reasonable to point at a real domain behind a reverse proxy (nginx, Caddy, Traefik) or deploy to any container host (Render, Railway, Fly.io, a VPS, etc.).
- `docker-compose.yml` **builds from your local source** rather than pulling prebuilt images, so any code changes you make are picked up on the next `docker compose up --build`.
- Never commit `backend/.env` — it's already in `.gitignore`. Commit `backend/.env.example` instead so collaborators know which variables to set.

## Roadmap ideas

If you want to take this further:
- Syntax highlighting in the code input (e.g. CodeMirror or Monaco), with issue lines highlighted inline
- Streaming responses instead of waiting for the full AI explanation
- Explanation/analysis history — save past snippets and results
- Support for other languages, not just Python (the `/explain` prompt path generalizes easily; the local `analyzer.py` is Python-specific since it's built on `ast`)
- A "Run it" sandbox to show real variable state alongside the flowchart, not just static structure
- Swap `GROQ_MODEL` for a smaller/faster model if you want higher throughput on the free tier

## License

Add a license of your choice (MIT is a common default for small projects like this).
