# Pythonic — Python Code Explainer

A small dockerized web app that explains Python code in plain English, powered by the Groq API (fast, free-tier LLM inference).

Paste a snippet of Python code into the browser, hit **Explain it**, and get a step-by-step, beginner-friendly explanation of what it does.

## How it works

- **Frontend** — a sleek black-and-yellow static page (served by nginx) with a code box and an explanation box.
- **Backend** — a Flask API with one main endpoint, `POST /explain`, which validates your input is real Python locally, then sends it to Groq with a tutoring-style prompt and returns the explanation as JSON.
- **Groq** — runs open models (Llama 3.3 70B by default) on custom LPU hardware; the app is a thin, focused wrapper around it.

```
Browser  --->  Flask backend (/explain)  --->  Groq API
   ^                                               |
   |________________ explanation __________________|
```

## Project structure

```
pce-improved/
├── backend/
│   ├── app.py            # Flask API
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

## Deploying

- The backend runs on **gunicorn** instead of the Flask dev server, so it's reasonable to point at a real domain behind a reverse proxy (nginx, Caddy, Traefik) or deploy to any container host (Render, Railway, Fly.io, a VPS, etc.).
- `docker-compose.yml` **builds from your local source** rather than pulling prebuilt images, so any code changes you make are picked up on the next `docker compose up --build`.
- Never commit `backend/.env` — it's already in `.gitignore`. Commit `backend/.env.example` instead so collaborators know which variables to set.

## Roadmap ideas

If you want to take this further:
- Syntax highlighting in the code input (e.g. CodeMirror or Monaco)
- Streaming responses instead of waiting for the full explanation
- Explanation history / save past snippets
- Support for other languages, not just Python
- Swap `GROQ_MODEL` for a smaller/faster model if you want higher throughput on the free tier

## License

Add a license of your choice (MIT is a common default for small projects like this).
