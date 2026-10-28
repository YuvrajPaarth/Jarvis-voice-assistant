"""
One-time setup for Jarvis.
Run this once: python3 setup_jarvis.py

Saves to ~/.jarvis/config.json with chmod 600 (owner-only).
Your API key and PIN are never printed back to the terminal.
"""

import os, json, hashlib, getpass

CONFIG_PATH = os.path.expanduser("~/.jarvis/config.json")

def load_existing():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)

cfg = load_existing()

print("=== Jarvis Setup ===")
print(f"Config will be saved to: {CONFIG_PATH}\n")

# --- Gemini API key ---
current_key = cfg.get("gemini_api_key", "")
if current_key:
    print(f"Gemini API key: already set (ends in ...{current_key[-6:]})")
    change = input("Change it? [y/N]: ").strip().lower()
    if change == "y":
        current_key = ""

if not current_key:
    key = getpass.getpass("Paste your Gemini API key (input hidden): ").strip()
    if key:
        cfg["gemini_api_key"] = key
        print("  ✓ API key saved.")
    else:
        print("  Skipped — online mode will be unavailable.")

# --- PIN ---
current_pin_hash = cfg.get("pin_hash", "")
if current_pin_hash:
    print("\nPIN: already set.")
    change = input("Change it? [y/N]: ").strip().lower()
    if change != "y":
        current_pin_hash = "keep"

if current_pin_hash != "keep":
    print("\nSet a voice PIN (you'll say the digits one by one, e.g. '1 2 3 4').")
    print("Leave blank to disable PIN lock.")
    pin = getpass.getpass("PIN digits (input hidden): ").strip()
    if pin:
        # confirm
        pin2 = getpass.getpass("Confirm PIN: ").strip()
        if pin == pin2:
            cfg["pin_hash"] = hashlib.sha256(pin.encode()).hexdigest()
            print("  ✓ PIN saved (stored as SHA-256 hash, never plain text).")
        else:
            print("  PINs didn't match — PIN not saved.")
    else:
        cfg.pop("pin_hash", None)
        print("  PIN lock disabled.")

save(cfg)
print(f"\n✓ Config saved to {CONFIG_PATH}")
print("  Run Jarvis with: python3 Jarvis.py")
