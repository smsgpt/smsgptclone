import os
from dotenv import load_dotenv
from flask import Flask, request
import requests
import hashlib
import time
from threading import Thread, Timer
from collections import defaultdict  # CONTEXT MEMORY

app = Flask(__name__)

# --- CONFIGURATION ---
load_dotenv()

# HuggingFace config
HF_TOKEN = os.getenv("HUGGINGFACE_API_KEY")
HF_MODEL_URL = "https://api-inference.huggingface.co/models/openchat/openchat-3.5"

# Telerivet config
TELERIVET_API_KEY = os.getenv("TELERIVET_API_KEY")
TELERIVET_PROJECT_ID = os.getenv("TELERIVET_PROJECT_ID")
TELERIVET_PHONE_ID = os.getenv("TELERIVET_PHONE_ID")

# Whitelist and trigger
whitelist_str = os.getenv("PHONE_NUMBER", "")
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

# Context memory
user_contexts = defaultdict(list)  # key = phone number, value = list of chat history
MAX_CONTEXT_LEN = 10  # Keep recent 10 exchanges only

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
        reply = get_huggingface_response(from_number, prompt)  # CONTEXT MEMORY
    except Exception as e:
        print(f"❗ HuggingFace error: {e}")
        reply = "⚠️ HuggingFace API is currently unavailable or rate-limited."

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


def get_huggingface_response(from_number, prompt):
    headers = {
        "Authorization": f"Bearer {HF_TOKEN}",
        "Content-Type": "application/json"
    }

    # Add new user prompt to memory
    user_contexts[from_number].append({"role": "user", "content": prompt})

    # Trim context if too long
    if len(user_contexts[from_number]) > MAX_CONTEXT_LEN:
        user_contexts[from_number] = user_contexts[from_number][-MAX_CONTEXT_LEN:]

    # Format conversation history into a single string
    conversation = ""
    for msg in user_contexts[from_number]:
        role = msg["role"]
        if role == "user":
            conversation += f"User: {msg['content']}\n"
        else:
            conversation += f"Assistant: {msg['content']}\n"
    conversation += "Assistant:"

    payload = {
        "inputs": conversation,
        "parameters": {
            "temperature": 0.7,
            "max_new_tokens": 300
        }
    }

    print("📡 Querying HuggingFace OpenChat...")
    r = requests.post(HF_MODEL_URL, headers=headers, json=payload)

    if r.status_code == 200:
        result = r.json()
        if isinstance(result, list):
            reply = result[0]['generated_text'].split("Assistant:")[-1].strip()
        elif "generated_text" in result:
            reply = result['generated_text'].split("Assistant:")[-1].strip()
        else:
            reply = str(result)

        # Add assistant reply to memory
        user_contexts[from_number].append({"role": "assistant", "content": reply})

        if len(reply) > MAX_SMS_CHARS:
            print(f"⚠️ Message too long ({len(reply)} chars), truncating.")
            reply = reply[:MAX_SMS_CHARS] + "\n[...truncated]"
        return reply
    else:
        print(f"❌ HF Error {r.status_code}: {r.text}")
        return "⚠️ HuggingFace API error. Try again later."


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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
