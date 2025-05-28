import os
from dotenv import load_dotenv
from flask import Flask, request
import requests
import hashlib
import time
from threading import Thread, Timer

app = Flask(__name__)

# --- CONFIGURATION ---
load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")  # Replace with your actual key
MODEL_NAME = "deepseek/deepseek-r1:free"


# Telerivet API info

TELERIVET_API_KEY = os.getenv("TELERIVET_API_KEY")
TELERIVET_PROJECT_ID = os.getenv("TELERIVET_PROJECT_ID")
TELERIVET_PHONE_ID = os.getenv("TELERIVET_PHONE_ID")

whitelist_str = os.getenv("WHITELIST", "")
WHITELIST = set(whitelist_str.split(",")) if whitelist_str else set()
TRIGGER_PREFIX = "Chat"

# SMS behavior
MAX_SMS_CHARS = 1200

# Message deduplication
recent_messages = {}  # key = from_number, value = (hash, timestamp)
REPEAT_TIMEOUT = 30   # seconds to ignore repeated messages

# Timers to delay sending SMS per user, to wait for the last part
send_timers = {}  # key = from_number, value = Timer object
pending_replies = {}  # key = from_number, value = (prompt, reply)


# --- ROUTES ---

@app.route("/incoming", methods=["POST"], strict_slashes=False)
def incoming():
    print(f"📩 Headers: {request.headers}")
    print(f"📩 Body: {request.get_data()}")

    if request.is_json:
        data = request.get_json()
    else:
        data = request.form.to_dict()

    if not data:
        print("❌ No data received.")
        return "Bad Request", 400

    from_number = data.get("from_number")
    content = data.get("content", "")

    if not from_number or from_number not in WHITELIST:
        print(f"⛔ Unauthorized sender: {from_number}")
        return "Unauthorized", 403

    if not content.strip().lower().startswith(TRIGGER_PREFIX.lower()):
        print("🚫 Ignoring non-GPT message.")
        return "Ignored", 200

    # Check for duplicate message
    msg_hash = hashlib.sha256(content.encode()).hexdigest()
    last_hash, last_time = recent_messages.get(from_number, (None, 0))

    if msg_hash == last_hash and time.time() - last_time < REPEAT_TIMEOUT:
        print("🔁 Duplicate message received recently. Ignoring.")
        return "Duplicate ignored", 200

    # Update the cache
    recent_messages[from_number] = (msg_hash, time.time())

    prompt = content[len(TRIGGER_PREFIX):].strip()
    print(f"✅ Prompt from {from_number}: {prompt}")

    # Start processing prompt async but delay sending SMS so only last response is sent
    Thread(target=process_prompt_with_delay, args=(from_number, prompt)).start()

    return "OK", 200


@app.route("/", methods=["GET"])
def home():
    return "Flask GPT-SMS server is running!", 200


# --- FUNCTIONS ---

def process_prompt_with_delay(from_number, prompt):
    try:
        reply = get_deepseek_response(prompt)
    except Exception as e:
        print(f"❗ DeepSeek error: {e}")
        reply = "⚠️ DeepSeek is currently unavailable or quota has been exceeded."

    # Store the latest reply for this user
    pending_replies[from_number] = reply

    # If a timer is already running, cancel it to reset the delay
    if from_number in send_timers:
        send_timers[from_number].cancel()

    # Start a new timer to send SMS after delay (e.g. 2 seconds)
    timer = Timer(2.0, send_pending_reply, args=(from_number,))
    send_timers[from_number] = timer
    timer.start()


def send_pending_reply(from_number):
    # Get and remove the pending reply for this user
    reply = pending_replies.pop(from_number, None)
    send_timers.pop(from_number, None)  # remove timer reference

    if reply:
        send_sms(from_number, reply)


def get_deepseek_response(prompt):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False  # Ensure streaming is disabled to avoid partial responses
    }

    print("📡 Querying DeepSeek via OpenRouter...")
    response = requests.post(url, json=payload, headers=headers)
    if response.status_code == 200:
        full_reply = response.json()['choices'][0]['message']['content'].strip()
        if len(full_reply) > MAX_SMS_CHARS:
            print(f"⚠️ Message too long ({len(full_reply)} chars), truncating.")
            full_reply = full_reply[:MAX_SMS_CHARS] + "\n[...truncated]"
        return full_reply
    else:
        print(f"❌ Error {response.status_code}: {response.text}")
        return "⚠️ DeepSeek API error. Try again later."


def send_sms(to_number, message):
    url = f"https://api.telerivet.com/v1/projects/{TELERIVET_PROJECT_ID}/messages/send"
    headers = {"Content-Type": "application/json"}
    auth = (TELERIVET_API_KEY, '')
    payload = {
        "to_number": to_number,
        "content": message,
        "phone_id": TELERIVET_PHONE_ID
    }

    print(f"📤 Sending SMS to {to_number}...")
    r = requests.post(url, json=payload, auth=auth, headers=headers)
    print(f"📬 Telerivet response: {r.status_code} - {r.text}")


# --- MAIN ---

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
