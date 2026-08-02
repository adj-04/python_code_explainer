from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import os
import ast
import logging
import threading
from datetime import date
from dotenv import load_dotenv

import analyzer  # local, offline AST analysis — no API calls, no quota impact

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

USE_GROQ = os.getenv("USE_GROQ", "false").lower() == "true"
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
MAX_CODE_LENGTH = int(os.getenv("MAX_CODE_LENGTH", "8000"))
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*")

# --- Quota / cost protection -------------------------------------------
# Per-IP rate limit, e.g. "10 per hour"
RATE_LIMIT = os.getenv("RATE_LIMIT", "10 per hour")
# Hard cap on total /explain calls per day, across ALL users combined.
# Groq's free tier caps out around 1,000-14,400 requests/day depending on
# model, so keep this comfortably under whatever your model's limit is.
MAX_REQUESTS_PER_DAY = int(os.getenv("MAX_REQUESTS_PER_DAY", "50"))

client = None
if USE_GROQ and GROQ_API_KEY:
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)
        logger.info(f"Groq client initialized with model {GROQ_MODEL}")
    except ModuleNotFoundError:
        logger.error("groq library not installed.")
        USE_GROQ = False
    except Exception as e:
        logger.error(f"Failed to initialize Groq client: {e}")
        USE_GROQ = False
else:
    logger.warning(
        "Groq is disabled or no API key was provided. "
        "Set USE_GROQ=true and GROQ_API_KEY in your .env file."
    )

app = Flask(__name__, static_folder="../frontend")
CORS(app, origins=ALLOWED_ORIGINS.split(",") if ALLOWED_ORIGINS != "*" else "*")

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

# --- Simple in-memory daily quota counter --------------------------------
_quota_lock = threading.Lock()
_quota_state = {"day": date.today(), "count": 0}


def _check_and_increment_daily_quota():
    with _quota_lock:
        today = date.today()
        if _quota_state["day"] != today:
            _quota_state["day"] = today
            _quota_state["count"] = 0
        if _quota_state["count"] >= MAX_REQUESTS_PER_DAY:
            return False
        _quota_state["count"] += 1
        return True


def _looks_like_python(code_text: str) -> bool:
    """
    Validates that the input is syntactically valid Python, entirely
    locally (no API call spent). Rejects prose, other languages, HTML,
    JSON, etc. before it ever reaches Groq.
    """
    try:
        tree = ast.parse(code_text)
    except SyntaxError:
        return False
    except (ValueError, TypeError):
        return False
    has_real_construct = any(
        isinstance(node, (
            ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
            ast.Import, ast.ImportFrom, ast.For, ast.While, ast.If,
            ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Return,
            ast.With, ast.Try, ast.Call,
        ))
        for node in ast.walk(tree)
    )
    return has_real_construct


@app.route("/health", methods=["GET"])
def health():
    with _quota_lock:
        remaining = max(0, MAX_REQUESTS_PER_DAY - _quota_state["count"])
    return jsonify({
        "status": "ok",
        "groq_enabled": bool(USE_GROQ and client is not None),
        "daily_quota_remaining": remaining,
    })


@app.route("/explain", methods=["POST"])
@limiter.limit(RATE_LIMIT)
def explain_code():
    if not USE_GROQ or client is None:
        return jsonify({
            "error": "Groq API is not enabled or API key is missing. "
                     "Set USE_GROQ=true and GROQ_API_KEY in backend/.env."
        }), 503

    data = request.get_json(silent=True)
    if not data or "code" not in data:
        return jsonify({"error": "No code provided. Send JSON: {\"code\": \"...\"}"}), 400

    code_text = data.get("code", "").strip()
    if not code_text:
        return jsonify({"error": "Empty code provided"}), 400

    if len(code_text) > MAX_CODE_LENGTH:
        return jsonify({
            "error": f"Code too long ({len(code_text)} chars). "
                     f"Max allowed is {MAX_CODE_LENGTH} characters."
        }), 413

    if not _looks_like_python(code_text):
        return jsonify({
            "error": "That doesn't look like valid Python code. "
                     "Paste actual Python (functions, loops, classes, etc.)."
        }), 422

    if not _check_and_increment_daily_quota():
        return jsonify({
            "error": "Daily request quota reached for this app. "
                     "Try again tomorrow, or raise MAX_REQUESTS_PER_DAY in backend/.env."
        }), 429

    try:
        prompt = (
            "You are a friendly, encouraging Python tutor. Explain the "
            "following Python code in simple, plain-English terms, like "
            "you're talking to someone learning to code. Walk through it "
            "step by step and call out anything tricky.\n\n"
            f"```python\n{code_text}\n```"
        )
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
        )
        explanation = (completion.choices[0].message.content or "").strip()
        if not explanation:
            return jsonify({"error": "Groq returned an empty response. Try again."}), 502
        return jsonify({"explanation": explanation})
    except Exception as e:
        logger.exception("Groq API call failed")
        return jsonify({"error": f"Groq API error: {str(e)}"}), 500


def _get_validated_code():
    """Shared input handling for the local-analysis endpoints.
    Returns (code_text, error_response_or_None).
    These endpoints never call Groq and never touch the daily quota."""
    data = request.get_json(silent=True)
    if not data or "code" not in data:
        return None, (jsonify({"error": "No code provided. Send JSON: {\"code\": \"...\"}"}), 400)

    code_text = data.get("code", "").strip()
    if not code_text:
        return None, (jsonify({"error": "Empty code provided"}), 400)

    if len(code_text) > MAX_CODE_LENGTH:
        return None, (jsonify({
            "error": f"Code too long ({len(code_text)} chars). "
                     f"Max allowed is {MAX_CODE_LENGTH} characters."
        }), 413)

    return code_text, None


# --- Local analysis endpoints -------------------------------------------
# None of these call Groq or touch the daily API quota. They run entirely
# on Python's built-in `ast` module, so they're free to use as often as
# needed and work even when USE_GROQ is disabled.

@app.route("/flowchart", methods=["POST"])
@limiter.limit(RATE_LIMIT)
def flowchart():
    code_text, err = _get_validated_code()
    if err:
        return err
    try:
        result = analyzer.generate_flowchart(code_text)
        return jsonify(result)
    except analyzer.AnalysisError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        logger.exception("Flowchart generation failed")
        return jsonify({"error": f"Could not analyze code: {e}"}), 500


@app.route("/issues", methods=["POST"])
@limiter.limit(RATE_LIMIT)
def issues():
    code_text, err = _get_validated_code()
    if err:
        return err
    try:
        result = analyzer.detect_issues(code_text)
        return jsonify({"issues": result})
    except analyzer.AnalysisError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        logger.exception("Issue detection failed")
        return jsonify({"error": f"Could not analyze code: {e}"}), 500


@app.route("/complexity", methods=["POST"])
@limiter.limit(RATE_LIMIT)
def complexity():
    code_text, err = _get_validated_code()
    if err:
        return err
    try:
        result = analyzer.compute_complexity(code_text)
        return jsonify({"complexity": result})
    except analyzer.AnalysisError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        logger.exception("Complexity analysis failed")
        return jsonify({"error": f"Could not analyze code: {e}"}), 500


@app.route("/explain-local", methods=["POST"])
@limiter.limit(RATE_LIMIT)
def explain_local():
    """Rule-based, line-by-line explanation. No Groq call, no quota impact."""
    code_text, err = _get_validated_code()
    if err:
        return err
    try:
        result = analyzer.explain_line_by_line(code_text)
        return jsonify(result)
    except analyzer.AnalysisError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        logger.exception("Local explain failed")
        return jsonify({"error": f"Could not analyze code: {e}"}), 500


@app.route("/optimize", methods=["POST"])
@limiter.limit(RATE_LIMIT)
def optimize():
    code_text, err = _get_validated_code()
    if err:
        return err
    try:
        result = analyzer.suggest_optimizations(code_text)
        return jsonify({"suggestions": result})
    except analyzer.AnalysisError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        logger.exception("Optimization analysis failed")
        return jsonify({"error": f"Could not analyze code: {e}"}), 500


@app.route("/analyze", methods=["POST"])
@limiter.limit(RATE_LIMIT)
def analyze_all():
    """Runs flowchart + issues + complexity + optimize in one call."""
    code_text, err = _get_validated_code()
    if err:
        return err
    try:
        return jsonify(analyzer.full_report(code_text))
    except analyzer.AnalysisError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        logger.exception("Full analysis failed")
        return jsonify({"error": f"Could not analyze code: {e}"}), 500


@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({"error": f"Too many requests. Limit: {RATE_LIMIT}."}), 429


@app.route("/", defaults={"path": "index.html"})
@app.route("/<path:path>")
def serve_frontend(path):
    try:
        return send_from_directory(app.static_folder, path)
    except Exception:
        return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
