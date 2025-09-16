from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

USE_GEMINI = os.getenv("USE_GEMINI", "false").lower() == "true"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Setup Gemini client
client = None
if USE_GEMINI and GEMINI_API_KEY:
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        client = genai.GenerativeModel("gemini-2.0-flash")
    except ModuleNotFoundError:
        print("Error: google-generativeai library not installed.")
        USE_GEMINI = False

# Flask app
app = Flask(__name__, static_folder="../frontend")
CORS(app)

@app.route("/explain", methods=["POST"])
def explain_code():
    data = request.get_json(silent=True)
    if not data or "code" not in data:
        return jsonify({"error": "No code provided"}), 400

    code_text = data.get("code", "").strip()
    if not code_text:
        return jsonify({"error": "Empty code provided"}), 400

    if USE_GEMINI and client:
        try:
            prompt = f"Explain the following Python code in simple terms:\n\n{code_text}\n\nExplanation:"
            response = client.generate_content(prompt)
            explanation = response.text.strip()
            return jsonify({"explanation": explanation})
        except Exception as e:
            return jsonify({"error": f"Gemini API error: {str(e)}"}), 500
    else:
        return jsonify({"error": "Gemini API is not enabled or API key is missing."}), 400

@app.route("/", defaults={"path": "index.html"})
@app.route("/<path:path>")
def serve_frontend(path):
    try:
        return send_from_directory(app.static_folder, path)
    except Exception:
        return send_from_directory(app.static_folder, "index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
