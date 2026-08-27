import sys
import json
import time
import ctypes
import ctypes.wintypes
import threading
import os
import requests
import re
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import pymem
import pymem.process
import webview

ntdll = ctypes.WinDLL('ntdll', use_last_error=True)

class IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [('Status', ctypes.c_int), ('Information', ctypes.c_void_p)]

NtWriteVirtualMemory = ntdll.NtWriteVirtualMemory
NtWriteVirtualMemory.argtypes = [ctypes.wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(IO_STATUS_BLOCK)]
NtWriteVirtualMemory.restype = ctypes.c_int

FFLAG_PREFIXES = {
    "DFString": 8, "FString": 7, "DFInt": 5, "FInt": 4,
    "DFLog": 5, "FLog": 4, "DFFlag": 6, "FFlag": 5,
}
_FLAG_PREFIXES = tuple(sorted(FFLAG_PREFIXES, key=len, reverse=True))
_BOOL_TYPES = frozenset(("FFlag", "DFFlag"))
_INT_TYPES = frozenset(("FInt", "DFInt", "FLog", "DFLog"))

OFFSET_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fflags_cache.json")
GITHUB_OFFSETS_URL = "https://raw.githubusercontent.com/mar1ontop/fflags/master/Offsets.hpp"
CACHE_DURATION_HOURS = 24
SAVED_FLAGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "saved_flags.json")

def clean_flag_name(flag_name):
    for prefix in _FLAG_PREFIXES:
        if flag_name.startswith(prefix):
            return flag_name[len(prefix):]
    return flag_name

def get_flag_type(flag_name):
    return next((prefix for prefix in _FLAG_PREFIXES if flag_name.startswith(prefix)), "unknown")

def find_roblox_processes():
    pids = []
    try:
        for p in pymem.process.list_processes():
            exe_name = p.szExeFile.decode('utf-8', 'ignore').lower()
            if p.th32ProcessID and 'robloxplayerbeta.exe' in exe_name:
                pids.append(p.th32ProcessID)
    except Exception:
        pass
    return pids

def get_module_base(pid):
    kernel32 = ctypes.windll.kernel32
    psapi    = ctypes.windll.psapi
    hProcess = kernel32.OpenProcess(0x400 | 0x10, False, pid)
    if not hProcess:
        return None
    hModules = (ctypes.c_void_p * 1024)()
    cbNeeded = ctypes.c_size_t()
    if psapi.EnumProcessModules(hProcess, ctypes.byref(hModules), ctypes.sizeof(hModules), ctypes.byref(cbNeeded)):
        if cbNeeded.value >= ctypes.sizeof(ctypes.c_void_p):
            kernel32.CloseHandle(hProcess)
            return int(hModules[0])
    kernel32.CloseHandle(hProcess)
    return None

def load_saved_flags():
    try:
        if os.path.exists(SAVED_FLAGS_FILE):
            with open(SAVED_FLAGS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                flags = data.get('flags', [])
                return [f for f in flags if isinstance(f, dict) and 'name' in f and 'value' in f] if isinstance(flags, list) else []
    except Exception:
        pass
    return []

def save_flags_to_file(flags):
    try:
        with open(SAVED_FLAGS_FILE, 'w', encoding='utf-8') as f:
            json.dump({'flags': flags, 'last_saved': datetime.now().isoformat()}, f, indent=2)
    except Exception:
        pass

def get_screen_center():
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        width = root.winfo_screenwidth()
        height = root.winfo_screenheight()
        root.destroy()
        return width, height
    except:
        return 1920, 1080

class OffsetManager:
    def __init__(self, log_cb=None):
        self._log = log_cb or print
        self._http = requests.Session()
        self.offsets = {}
        self.last_update = None
        self.load_from_cache()
    
    def load_from_cache(self):
        try:
            if os.path.exists(OFFSET_CACHE_FILE):
                with open(OFFSET_CACHE_FILE, 'r') as f:
                    data = json.load(f)
                    self.offsets = data.get('offsets', {})
                    self.last_update = datetime.fromisoformat(data.get('last_update', '2000-01-01'))
                    self._log(f"- Loaded {len(self.offsets)} offsets from cache")
                    return True
        except Exception as e:
            self._log(f"- Failed to load cache: {e}")
        return False
    
    def save_to_cache(self):
        try:
            data = {
                'offsets': self.offsets,
                'last_update': datetime.now().isoformat()
            }
            with open(OFFSET_CACHE_FILE, 'w') as f:
                json.dump(data, f)
            self._log(f"- Saved {len(self.offsets)} offsets to cache")
        except Exception as e:
            self._log(f"- Failed to save cache: {e}")
    
    def is_cache_fresh(self):
        if not self.last_update:
            return False
        return datetime.now() - self.last_update < timedelta(hours=CACHE_DURATION_HOURS)
    
    def sync_from_github(self):
        self._log("- Syncing offsets from GitHub...")
        try:
            r = self._http.get(GITHUB_OFFSETS_URL, timeout=10)
            if r.status_code != 200:
                self._log(f"- GitHub returned {r.status_code}")
                return False
            
            r.raise_for_status()
            content = r.text
            matches = re.findall(r'uintptr_t\s+(\w+)\s*=\s*(0x[0-9A-Fa-f]+);', content)
            
            if not matches:
                self._log("- No offsets found in file")
                return False
            
            self.offsets = {name: int(offset, 16) for name, offset in matches}
            self.save_to_cache()
            self._log(f"- Synced {len(self.offsets)} offsets")
            return True
        except Exception as e:
            self._log(f"- Sync failed: {e}")
            return False
    
    def get_offset(self, flag_name):
        clean = clean_flag_name(flag_name)
        return self.offsets.get(clean)
    
    def get_all_offsets(self):
        return self.offsets


class OffsetInjector:
    def __init__(self, log_cb=None):
        self.k32 = ctypes.windll.kernel32
        self.pid = 0
        self.h_process = None
        self.pm = None
        self.base = 0
        self._log = log_cb or print
        self.offset_manager = OffsetManager(log_cb)
        self.default_values = {}
        self.injected_flags_cache = {}
    
    def attach_with_handle(self, pid, pm, base):
        try:
            self.pid = pid
            self.pm = pm
            self.base = base
            self.h_process = pm.process_handle
            self._log(f"- Attached to PID {pid}")
            return True
        except Exception:
            return False
    
    def _write_memory(self, address, data):
        buf = (ctypes.c_char * len(data))(*data)
        written = ctypes.c_size_t(0)
        ok = self.k32.WriteProcessMemory(self.h_process, ctypes.c_void_p(address), buf, len(data), ctypes.byref(written))
        return bool(ok) and written.value == len(data)
    
    def set_flag(self, full_name, value):
        offset = self.offset_manager.get_offset(full_name)
        if offset is None:
            return False
        
        addr = self.base + offset
        
        val_s = str(value)
        flag_type = get_flag_type(full_name)
        
        try:
            if flag_type in ("FFlag", "DFFlag"):
                bool_val = 1 if val_s.lower() in ("true","1","yes") else 0
                result = self._write_memory(addr, bool_val.to_bytes(4, 'little', signed=True))
                if result:
                    self.injected_flags_cache[full_name] = bool_val
                return result
            elif flag_type in ("FInt", "DFInt", "FLog", "DFLog"):
                int_val = int(value)
                result = self._write_memory(addr, int_val.to_bytes(4, 'little', signed=True))
                if result:
                    self.injected_flags_cache[full_name] = int_val
                return result
            elif flag_type in ("FString", "DFString"):
                enc = val_s.encode('utf-8') + b'\x00'
                result = self._write_memory(addr, enc)
                if result:
                    self.injected_flags_cache[full_name] = val_s
                return result
            else:
                if val_s.lower() in ("true", "false"):
                    bool_val = 1 if val_s.lower() == "true" else 0
                    result = self._write_memory(addr, bool_val.to_bytes(4, 'little', signed=True))
                    if result:
                        self.injected_flags_cache[full_name] = bool_val
                    return result
                elif val_s.lstrip('-').isdigit():
                    int_val = int(val_s)
                    result = self._write_memory(addr, int_val.to_bytes(4, 'little', signed=True))
                    if result:
                        self.injected_flags_cache[full_name] = int_val
                    return result
                else:
                    enc = val_s.encode('utf-8') + b'\x00'
                    result = self._write_memory(addr, enc)
                    if result:
                        self.injected_flags_cache[full_name] = val_s
                    return result
        except Exception as e:
            self._log(f"- Write failed for {full_name}: {e}")
            return False
    
    def verify_flag(self, full_name, expected_value):
        offset = self.offset_manager.get_offset(full_name)
        if offset is None:
            return False
        
        addr = self.base + offset
        flag_type = get_flag_type(full_name)
        
        try:
            if flag_type in ("FFlag", "DFFlag"):
                val = self.pm.read_int(addr)
                expected_bool = 1 if str(expected_value).lower() in ("true","1","yes") else 0
                return val == expected_bool
            elif flag_type in ("FInt", "DFInt", "FLog", "DFLog"):
                val = self.pm.read_int(addr)
                return val == int(expected_value)
            else:
                val = self.pm.read_string(addr, 64)
                return str(val).strip() == str(expected_value).strip()
        except:
            return False
    
    def get_flag(self, full_name):
        offset = self.offset_manager.get_offset(full_name)
        if offset is None:
            return None
        
        addr = self.base + offset
        
        try:
            flag_type = get_flag_type(full_name)
            if flag_type in _BOOL_TYPES:
                val = self.pm.read_int(addr)
                return "True" if val else "False"
            elif flag_type in _INT_TYPES:
                val = self.pm.read_int(addr)
                return str(val)
            else:
                return self.pm.read_string(addr, 64)
        except:
            return None
    
    def store_default_value(self, full_name):
        try:
            value = self.get_flag(full_name)
            if value is not None:
                self.default_values[full_name] = value
        except Exception:
            pass
    
    def restore_to_default(self, flags):
        success_count = 0
        for flag in flags:
            try:
                name = flag["name"]
                if name in self.default_values:
                    if self.set_flag(name, self.default_values[name]):
                        success_count += 1
            except Exception:
                continue
        return success_count
    
    def get_injected_flags_cache(self):
        return self.injected_flags_cache


class FFlagMonitor:
    def __init__(self, injector, log_cb=None):
        self.injector = injector
        self._log = log_cb or print
        self.flag_states = {}
        self.monitoring = False
        self.monitor_thread = None
        self.check_interval = 0.5
        self.total_reinjected = 0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    def start_monitoring(self):
        if self.monitoring:
            return
        self.monitoring = True
        self._stop_event.clear()
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()

    def stop_monitoring(self):
        self.monitoring = False
        self._stop_event.set()

    def update_injected_flags(self, flags):
        with self._lock:
            self.flag_states.clear()
            for flag in flags:
                name = flag["name"]
                self.flag_states[name] = {
                    'desired': flag["value"],
                    'last_reinject': time.time(),
                    'injection_count': 1
                }

    def clear_injected_flags(self):
        with self._lock:
            self.flag_states.clear()
            self.total_reinjected = 0

    def get_total_reinjected(self):
        with self._lock:
            return self.total_reinjected

    def _monitor_loop(self):
        if self._stop_event.wait(2):
            return
        while self.monitoring:
            try:
                if not self.flag_states or not self.injector.h_process:
                    if self._stop_event.wait(1):
                        break
                    continue
                self._check_and_fix_changes()
                if self._stop_event.wait(self.check_interval):
                    break
            except Exception:
                if self._stop_event.wait(5):
                    break

    def _check_and_fix_changes(self):
        flags_to_reinject = []
        
        with self._lock:
            snapshot = [(name, state['desired']) for name, state in self.flag_states.items()]

        # Process reads can be slow; do not hold the state lock during I/O.
        for name, desired in snapshot:
            try:
                if not self.injector.verify_flag(name, desired):
                    flags_to_reinject.append(name)
            except Exception:
                continue
        
        for name in flags_to_reinject:
            try:
                with self._lock:
                    state = self.flag_states.get(name)
                if not state:
                    continue
                
                desired = state['desired']
                
                if self.injector.set_flag(name, desired):
                    with self._lock:
                        current = self.flag_states.get(name)
                        if current is not None:
                            current['last_reinject'] = time.time()
                            current['injection_count'] += 1
                            self.total_reinjected += 1
            except Exception:
                continue


class HybridInjector:
    def __init__(self, log_cb=None):
        self._log = log_cb or print
        self.connected_processes = {}
        self.offset_injector = None
        self.monitor = None
        self.monitoring_enabled = True
        self.saved_flags = []
        self._waiting_message_shown = False
        self._is_injecting = False
        
        self.saved_flags = load_saved_flags()
        if self.saved_flags:
            self._log(f"- Loaded {len(self.saved_flags)} saved flag(s) from previous session")
        
        self.offset_manager = OffsetManager(log_cb)
        if not self.offset_manager.is_cache_fresh():
            self.offset_manager.sync_from_github()
        
        threading.Thread(target=self._monitor_roblox, daemon=True).start()

    def sync_offsets(self):
        return self.offset_manager.sync_from_github()

    def _monitor_roblox(self):
        last_pids = set()
        while True:
            try:
                current_pids = set(find_roblox_processes())
                new_pids = current_pids - last_pids
                lost_pids = last_pids - current_pids
                
                for pid in lost_pids:
                    if pid in self.connected_processes:
                        del self.connected_processes[pid]
                        self._log(f"- Roblox process {pid} closed")
                    self._is_injecting = False
                
                for pid in new_pids:
                    try:
                        self._log(f"- New Roblox process detected (PID: {pid})")
                        pm = pymem.Pymem(pid)
                        base = get_module_base(pid)
                        if base:
                            self.connected_processes[pid] = {'pm': pm, 'base': base}
                            self.offset_injector = OffsetInjector(log_cb=self._log)
                            self.offset_injector.attach_with_handle(pid, pm, base)
                            self.monitor = FFlagMonitor(self.offset_injector, log_cb=self._log)
                            if self.monitoring_enabled:
                                self.monitor.start_monitoring()
                            
                            if self.saved_flags and not self._is_injecting:
                                self._is_injecting = True
                                self._log(f"- Reinjecting {len(self.saved_flags)} saved flag(s)...")
                                result = self._apply_offset(self.saved_flags, silent=True)
                                if result['success'] > 0:
                                    self._log(f"- Reinjected {result['success']}/{result['success']+result['fail']} flags")
                                    _window.evaluate_js(f"window.setFlagCount({result['success']})")
                                self._is_injecting = False
                    except Exception as e:
                        self._log(f"- Failed to attach to PID {pid}: {e}")
                        continue
                
                last_pids = current_pids
                
                if not current_pids and not self._waiting_message_shown:
                    self._log("- Waiting for Roblox...")
                    self._waiting_message_shown = True
                elif current_pids and self._waiting_message_shown:
                    self._waiting_message_shown = False
                    
            except Exception as e:
                pass
            time.sleep(2)

    def uninject_current(self):
        if not self.connected_processes:
            return {'success': 0, 'fail': 0, 'message': 'No Roblox process attached'}
        if self.saved_flags and self.offset_injector:
            restored = self.offset_injector.restore_to_default(self.saved_flags)
            self.saved_flags = []
            save_flags_to_file([])
            if self.monitor:
                self.monitor.clear_injected_flags()
            return {'success': restored, 'fail': 0, 'message': f'Restored {restored} flags to default'}
        return {'success': 0, 'fail': 0, 'message': 'No flags to uninject'}

    def apply_flags(self, flags):
        if not self.connected_processes:
            self.saved_flags = flags
            save_flags_to_file(flags)
            return {'success': 0, 'fail': len(flags), 'message': 'Roblox not running. Flags saved for next launch.'}
        
        if self.saved_flags:
            self.uninject_current()
            if self.monitor:
                self.monitor.clear_injected_flags()
        
        result = self._apply_offset(flags, silent=False)
        if result['success'] > 0:
            self.saved_flags = flags
            save_flags_to_file(flags)
        return result

    def _apply_offset(self, flags, silent=False):
        ok = 0
        fail = 0
        injected = []
        failed = []
        
        for flag in flags:
            name = flag["name"]
            value = flag["value"]
            
            self.offset_injector.store_default_value(name)
            
            if self.offset_injector.set_flag(name, value):
                ok += 1
                injected.append(flag)
            else:
                fail += 1
                failed.append(flag)
        
        if not silent:
            self._log(f"- Done: {ok}/{ok+fail} flags applied, {fail} failed")
        
        if failed and not silent:
            unknown = [f['name'] for f in failed]
            self._log(f"[!] {len(failed)} flag(s) failed (no offset in cache): {', '.join(unknown[:5])}")
        
        if self.monitor and self.monitoring_enabled and ok > 0:
            self.monitor.update_injected_flags(injected)
            if not self.monitor.monitoring:
                self.monitor.start_monitoring()
        
        return {'success': ok, 'fail': fail, 'message': f'Injected {ok}/{ok+fail} flags'}

    def toggle_monitoring(self):
        if not self.monitor:
            return False
        if self.monitor.monitoring:
            self.monitor.stop_monitoring()
            self.monitoring_enabled = False
        else:
            self.monitor.start_monitoring()
            self.monitoring_enabled = True
        return self.monitor.monitoring

    def get_reinjected_count(self):
        return self.monitor.get_total_reinjected() if self.monitor else 0

    def is_attached(self):
        return bool(self.connected_processes)


_window = None

def push(msg: str):
    if _window:
        escaped = msg.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n')
        _window.evaluate_js(f"window.appendLog('{escaped}')")


injector = None

def get_injector():
    global injector
    if injector is None:
        injector = HybridInjector(log_cb=push)
    return injector


class Api:
    def close_app(self):
        if _window:
            _window.destroy()
        return {'ok': True}

    def load_flags(self):
        result = _window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=False,
            file_types=('JSON Files (*.json)', 'All files (*.*)')
        )
        if not result:
            return {'ok': False, 'message': 'Cancelled'}
        path = result[0]
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            data = json.loads(content)
            flags = []
            for k, v in data.items():
                flags.append({'name': k, 'value': str(v)})
            fname = os.path.basename(path)
            push(f"- Loaded: {fname} ({len(data):,} flags)")
            push("- Injecting flags...")
            
            def worker():
                res = get_injector().apply_flags(flags)
                if res['success'] > 0:
                    push(f"- {res['success']}/{res['success']+res['fail']} flags applied")
                    _window.evaluate_js(f"window.setFlagCount({res['success']})")
                else:
                    push(f"- {res['message']}")
            
            threading.Thread(target=worker, daemon=True).start()
            return {'ok': True, 'message': f'Loaded {len(data)} flags', 'file': fname}
        except json.JSONDecodeError as e:
            push(f"- Invalid JSON: {e}")
            return {'ok': False, 'message': f'Invalid JSON: {e}'}
        except Exception as e:
            push(f"- File error: {e}")
            return {'ok': False, 'message': str(e)}

    def uninject(self):
        def worker():
            res = get_injector().uninject_current()
            if res['success'] > 0:
                push(f"- Restored {res['success']} flags to default")
                _window.evaluate_js("window.setFlagCount(0)")
            else:
                push(f"- {res['message']}")
        threading.Thread(target=worker, daemon=True).start()
        return {'ok': True}

    def sync_offsets(self):
        def worker():
            push("- Syncing offsets from GitHub...")
            success = get_injector().sync_offsets()
            if success:
                push("- Offsets synced successfully")
            else:
                push("- Failed to sync offsets")
        threading.Thread(target=worker, daemon=True).start()
        return {'ok': True}

    def toggle_monitor(self):
        is_on = get_injector().toggle_monitoring()
        return {'monitoring': is_on}

    def get_status(self):
        current = get_injector()
        return {
            'attached': current.is_attached(),
            'reinjected': current.get_reinjected_count(),
            'monitoring': current.monitor.monitoring if current.monitor else False,
            'flags': len(current.saved_flags),
        }

    def clear_log(self):
        if _window:
            _window.evaluate_js("window.clearLog()")
        return {'ok': True}

    def minimize_window(self):
        if _window:
            _window.minimize()
        return {'ok': True}

    def close_window(self):
        if _window:
            _window.destroy()
        return {'ok': True}


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>Hidden</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet"/>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-font-smoothing:antialiased}

:root{
  --bg:        #000000;
  --surface:   rgba(10,10,10,0.06);
  --border:    rgba(26,26,26,0.2);
  --border2:   rgba(42,42,42,0.2);
  --accent:    #ffffff;
  --white:     #ffffff;
  --muted:     #cccccc;
  --green:     #ffffff;
  --blue:      #ffffff;
  --red:       #ffffff;
  --yellow:    #ffffff;
  --mono:      'Space Mono', monospace;
  --sans:      'DM Sans', sans-serif;
}

html,body{
  width:100%;height:100%;
  margin:0;
  background-color:#000;
  color:var(--white);
  font-family:var(--sans);
  overflow:hidden;
  user-select:none;
  font-size:14px;
}

#blackhole-container {
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 340px;
  height: 340px;
  pointer-events: none;
  z-index: 0;
}

#blackhole-canvas {
  width: 100%;
  height: 100%;
  display: block;
}

#titlebar,
#status-strip,
#log-container,
#toolbar,
.credit-bar {
  background: rgba(0,0,0,0.06);
  backdrop-filter: blur(1px);
  -webkit-backdrop-filter: blur(1px);
  position: relative;
  z-index: 1;
}

#titlebar{
  display:flex;align-items:center;justify-content:space-between;
  padding:0 16px;height:48px;
  border-bottom:1px solid var(--border);
  -webkit-app-region:drag;
  flex-shrink:0;
}
#titlebar .brand{
  display:flex;align-items:center;gap:10px;
  -webkit-app-region:no-drag;
}
#titlebar .logo{
  width:24px; height:24px;
  background: #000;
  border-radius: 50%;
  border: 2px solid #fff;
  box-shadow: 0 0 8px rgba(255,255,255,0.4);
}
#titlebar .name{
  font-family:var(--mono);
  font-size:15px;
  font-weight:700;
  letter-spacing:.1em;
  color:var(--white);
}
#titlebar .name span{color:var(--accent);}
#titlebar .controls{
  display:flex;align-items:center;gap:6px;
  -webkit-app-region:no-drag;
}
.wbtn{
  width:32px;height:32px;
  border-radius:6px;
  border:none;cursor:pointer;
  background:transparent;color:var(--muted);
  font-size:16px;
  display:flex;align-items:center;justify-content:center;
  transition:all .15s;
}
.wbtn:hover{background:rgba(255,255,255,0.12);color:var(--white);}
.wbtn.close:hover{background:#ff4757;color:#fff;}
.wbtn.clear-btn{
  font-family:var(--mono);
  font-size:11px;
  font-weight:700;
  letter-spacing:.08em;
  padding:0 12px;
  width:auto;
  color:#ccc;
}
.wbtn.clear-btn:hover{color:var(--white);background:rgba(255,255,255,0.12);}

#app{display:flex;flex-direction:column;height:calc(100vh - 48px);position:relative;z-index:1;}

#status-strip{
  display:flex;align-items:center;gap:0;
  padding:0 16px;height:42px;
  border-bottom:1px solid var(--border);
  flex-shrink:0;
}
.stat-pill{
  display:flex;align-items:center;gap:8px;
  padding:0 14px;height:100%;
  border-right:1px solid var(--border);
  font-size:12px;
  font-family:var(--mono);
  letter-spacing:.05em;
  color:var(--muted);
}
.stat-pill:first-child{padding-left:0;}
.stat-pill .dot{
  width:8px;height:8px;
  border-radius:50%;
  background:var(--muted);
  transition:background .3s;
}
.stat-pill.on .dot{background:var(--green);box-shadow:0 0 6px var(--green);}
.stat-pill.on{color:var(--white);}
.stat-pill .val{color:var(--accent);font-weight:700;font-size:13px;}

#log-container{
  flex:1;
  position:relative;
  display:flex;
  flex-direction:column;
  overflow:hidden;
}
#log{
  flex:1;
  overflow-y:auto;
  overflow-x:hidden;
  padding:14px 18px;
  font-family:var(--mono);
  font-size:12px;
  line-height:1.9;
  background:rgba(0,0,0,0.02);
  backdrop-filter: blur(0.5px);
  -webkit-backdrop-filter: blur(0.5px);
}
#log::-webkit-scrollbar{width:6px;}
#log::-webkit-scrollbar-track{background:transparent;}
#log::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.08);border-radius:3px;}

.log-line{
  display:flex;gap:12px;align-items:flex-start;
  animation:fadeIn .15s ease;
  white-space:pre-wrap;word-break:break-word;
  margin-bottom:4px;
}
@keyframes fadeIn{from{opacity:0;transform:translateY(2px)}to{opacity:1;transform:none}}
.log-line .ts{
  color:#888;
  flex-shrink:0;
  font-size:11px;
}
.log-line.ok .txt{color:var(--green);}
.log-line.err .txt{color:var(--red);}
.log-line.warn .txt{color:var(--yellow);}
.log-line.info .txt{color:var(--blue);}
.log-line.def .txt{color:#ccc;}

.credit-bar{
  text-align:right;
  padding:6px 18px;
  border-top:1px solid var(--border);
  flex-shrink:0;
}
.credit-text{
  color:#888;
  font-size:10px;
  font-family:var(--mono);
  letter-spacing:1px;
}

#toolbar{
  display:flex;gap:8px;align-items:center;
  padding:12px 16px;
  border-top:1px solid var(--border);
  flex-shrink:0;
}
.btn{
  display:flex;align-items:center;gap:8px;
  padding:0 16px;height:38px;
  border-radius:6px;
  border:1px solid var(--border2);
  background:rgba(0,0,0,0.08);
  color:var(--muted);
  font-family:var(--mono);
  font-size:11px;
  font-weight:700;
  letter-spacing:.08em;
  cursor:pointer;
  transition:all .15s;
  white-space:nowrap;
  text-transform:uppercase;
}
.btn:hover{border-color:var(--accent);color:var(--white);background:rgba(255,255,255,0.10);}
.btn:active{transform:scale(.97);}
.btn:disabled{opacity:.35;cursor:not-allowed;pointer-events:none;}
.btn svg{flex-shrink:0;width:14px;height:14px;}
.btn.primary{
  border-color:var(--accent);
  color:var(--accent);
}
.btn.primary:hover{background:var(--accent);color:#000;}
.btn.danger{border-color:rgba(255,255,255,0.3);}
.btn.danger:hover{border-color:#fff;color:#fff;background:rgba(255,255,255,0.08);}
#spacer{flex:1;}
</style>
</head>
<body>

<div id="blackhole-container">
  <canvas id="blackhole-canvas"></canvas>
</div>

<div id="titlebar">
  <div class="brand">
    <div class="logo"></div>
    <div class="name">OBLI<span>VION</span></div>
  </div>
  <div class="controls">
    <button class="wbtn clear-btn" id="clearBtn">Clear</button>
    <button class="wbtn" id="minBtn">−</button>
    <button class="wbtn close" id="closeBtn">×</button>
  </div>
</div>

<div id="app">
  <div id="status-strip">
    <div class="stat-pill" id="pill-attach">
      <div class="dot"></div>
      <span>ROBLOX</span>
    </div>
    <div class="stat-pill" id="pill-monitor">
      <div class="dot"></div>
      <span>MONITOR</span>
    </div>
    <div class="stat-pill">
      <span>FLAGS&nbsp;</span><span class="val" id="stat-flags">0</span>
    </div>
    <div class="stat-pill">
      <span>REINJECTED&nbsp;</span><span class="val" id="stat-reinject">0</span>
    </div>
  </div>

  <div id="log-container">
    <div id="log">
      <div class="log-line info"><span class="ts">00:00:00</span><span class="txt">Hidden initialised — waiting for Roblox</span></div>
    </div>
    <div class="credit-bar">
      <div class="credit-text">Made by mar1ontop</div>
    </div>
  </div>

  <div id="toolbar">
    <button class="btn primary" onclick="doLoad()">
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 1v8M4 6l3 3 3-3M1 12h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
      Load Flags
    </button>
    <button class="btn" onclick="doSync()">
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M11.5 7A4.5 4.5 0 1 1 7 2.5M11.5 2.5v4.5H7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
      Sync Offsets
    </button>
    <button class="btn" id="btn-monitor" onclick="toggleMonitor()">
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><rect x="1.5" y="3.5" width="11" height="7.5" rx="1.5" stroke="currentColor" stroke-width="1.5"/><path d="M4.5 11.5v1.5M9.5 11.5v1.5M3.5 13.5h7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
      <span id="monitor-label">Stop Monitor</span>
    </button>
    <div id="spacer"></div>
    <button class="btn danger" onclick="doUninject()">
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M2 2l10 10M12 2L2 12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
      Uninject
    </button>
  </div>
</div>

<script>
'use strict';

const canvas = document.getElementById('blackhole-canvas');
const container = document.getElementById('blackhole-container');
const ctx = canvas.getContext('2d');

function resizeCanvas() {
  const rect = container.getBoundingClientRect();
  canvas.width = rect.width * window.devicePixelRatio;
  canvas.height = rect.height * window.devicePixelRatio;
  canvas.style.width = rect.width + 'px';
  canvas.style.height = rect.height + 'px';
  ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
}
resizeCanvas();
window.addEventListener('resize', resizeCanvas);

let orbitAngle = 0;

const numParticles = 120;
const particles = [];
for (let i = 0; i < numParticles; i++) {
  const type = Math.random() < 0.7 ? 'disk' : 'ring';
  let radiusBase;
  if (type === 'disk') {
    radiusBase = 0.45 + Math.random() * 0.45;
  } else {
    radiusBase = 1.02 + Math.random() * 0.08;
  }
  const angleOffset = Math.random() * 2 * Math.PI;
  const speed = 0.0018 + Math.random() * 0.0022;
  const size = 0.6 + Math.random() * 1.6;
  const opacity = 0.4 + Math.random() * 0.6;
  particles.push({
    type,
    radiusBase,
    angleOffset,
    speed,
    size,
    opacity,
    phase: Math.random() * 100
  });
}

function drawParticles(time) {
  const w = canvas.width / window.devicePixelRatio;
  const h = canvas.height / window.devicePixelRatio;
  const cx = w/2, cy = h/2;
  const R = Math.min(w, h) * 0.40;
  const tilt = 20 * Math.PI / 180;
  const flatten = 0.38;
  const offsetY = R * 0.08;

  for (const p of particles) {
    let angle = p.angleOffset + time * p.speed * 0.001;
    if (p.type === 'ring') {
      angle += 0.3 * Math.sin(time * 0.0015 + p.phase);
    }
    const radius = R * p.radiusBase;

    const x0 = radius * Math.cos(angle);
    const y0 = radius * Math.sin(angle);

    const cosT = Math.cos(tilt);
    const sinT = Math.sin(tilt);
    let px = x0 * cosT - y0 * sinT;
    let py = (x0 * sinT + y0 * cosT) * flatten;

    if (p.type === 'ring') {
      py -= offsetY;
    }

    px += cx;
    py += cy;

    const size = p.size * (0.8 + 0.2 * Math.sin(time * 0.002 + p.phase));
    const alpha = p.opacity * (0.8 + 0.2 * Math.sin(time * 0.0015 + p.phase * 1.2));

    ctx.beginPath();
    ctx.arc(px, py, size, 0, Math.PI*2);
    ctx.fillStyle = `rgba(255,255,255,${alpha})`;
    ctx.fill();
  }
}

function drawBlackHole(time) {
  const w = canvas.width / window.devicePixelRatio;
  const h = canvas.height / window.devicePixelRatio;
  const cx = w/2, cy = h/2;
  const R = Math.min(w, h) * 0.40;

  ctx.clearRect(0, 0, w, h);

  const offsetY = R * 0.08;
  const shadowCenterX = cx;
  const shadowCenterY = cy - offsetY;
  const shadowR = R * 0.40;
  ctx.beginPath();
  ctx.arc(shadowCenterX, shadowCenterY, shadowR, 0, Math.PI*2);
  ctx.fillStyle = 'rgba(0,0,0,1)';
  ctx.fill();

  const ringR = shadowR * 1.0;
  ctx.shadowColor = 'rgba(255,255,255,0.6)';
  ctx.shadowBlur = 25;
  ctx.beginPath();
  ctx.arc(shadowCenterX, shadowCenterY, ringR, 0, Math.PI*2);
  ctx.strokeStyle = 'rgba(255,255,255,1)';
  ctx.lineWidth = 2.5;
  ctx.stroke();
  ctx.shadowBlur = 0;

  const outerRingR = shadowR * 1.12;
  ctx.beginPath();
  ctx.arc(shadowCenterX, shadowCenterY, outerRingR, 0, Math.PI*2);
  ctx.strokeStyle = 'rgba(255,255,255,0.15)';
  ctx.lineWidth = 1.5;
  ctx.stroke();

  ctx.save();
  ctx.shadowBlur = 0;
  const lensOff = 4;
  ctx.beginPath();
  ctx.arc(cx + lensOff, cy, ringR * 1.08, 0, Math.PI*2);
  ctx.strokeStyle = 'rgba(255,255,255,0.06)';
  ctx.lineWidth = 3;
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(cx - lensOff, cy, ringR * 1.08, 0, Math.PI*2);
  ctx.strokeStyle = 'rgba(255,255,255,0.04)';
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.restore();

  const coronaGrad = ctx.createRadialGradient(cx, cy, ringR*0.9, cx, cy, ringR*1.8);
  coronaGrad.addColorStop(0, 'rgba(255,255,255,0)');
  coronaGrad.addColorStop(0.5, 'rgba(255,255,255,0.04)');
  coronaGrad.addColorStop(1, 'rgba(255,255,255,0)');
  ctx.beginPath();
  ctx.arc(cx, cy, ringR*1.8, 0, Math.PI*2);
  ctx.fillStyle = coronaGrad;
  ctx.fill();

  drawParticles(time);
}

function animate(time) {
  orbitAngle += 0.01;
  drawBlackHole(time);
  requestAnimationFrame(animate);
}
animate(0);

let monitorOn = true;

function ts(){
  const d = new Date();
  return [d.getHours(),d.getMinutes(),d.getSeconds()].map(n=>String(n).padStart(2,'0')).join(':');
}

window.appendLog = function(msg){
  const log = document.getElementById('log');
  const line = document.createElement('div');
  let cls = 'def';
  if(msg.startsWith('-')) cls='ok';
  else if(msg.startsWith('-')) cls='err';
  else if(msg.startsWith('-')) cls='warn';
  else if(msg.startsWith('-')) cls='info';
  line.className = 'log-line ' + cls;
  line.innerHTML = `<span class="ts">${ts()}</span><span class="txt">${escapeHtml(msg)}</span>`;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
};

window.clearLog = function(){
  const log = document.getElementById('log');
  log.innerHTML = '<div class="log-line info"><span class="ts">00:00:00</span><span class="txt">Hidden initialised — waiting for Roblox</span></div>';
};

function escapeHtml(s){
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

window.setFlagCount = function(n){
  document.getElementById('stat-flags').textContent = n;
};

function pollStatus(){
  if(!window.pywebview) return;
  window.pywebview.api.get_status().then(s => {
    document.getElementById('pill-attach').classList.toggle('on', s.attached);
    document.getElementById('pill-monitor').classList.toggle('on', s.monitoring);
    document.getElementById('stat-flags').textContent = s.flags;
    document.getElementById('stat-reinject').textContent = s.reinjected;
    monitorOn = s.monitoring;
    document.getElementById('monitor-label').textContent = monitorOn ? 'Stop Monitor' : 'Start Monitor';
  }).catch(()=>{});
}
setInterval(pollStatus, 1500);

function doLoad(){
  if(!window.pywebview) return;
  window.pywebview.api.load_flags();
}
function doSync(){
  if(!window.pywebview) return;
  window.pywebview.api.sync_offsets();
}
function doUninject(){
  if(!window.pywebview) return;
  window.pywebview.api.uninject();
}
function toggleMonitor(){
  if(!window.pywebview) return;
  window.pywebview.api.toggle_monitor().then(r=>{
    monitorOn = r.monitoring;
    document.getElementById('monitor-label').textContent = monitorOn ? 'Stop Monitor' : 'Start Monitor';
  });
}

document.getElementById('clearBtn').addEventListener('click', () => {
  if(window.pywebview) window.pywebview.api.clear_log();
});
document.getElementById('minBtn').addEventListener('click', () => {
  if(window.pywebview) window.pywebview.api.minimize_window();
});
document.getElementById('closeBtn').addEventListener('click', () => {
  if(window.pywebview) window.pywebview.api.close_window();
});
</script>
</body>
</html>
"""


def main():
    global _window
    get_injector()
    api = Api()
    
    screen_width, screen_height = get_screen_center()
    window_width, window_height = 850, 550
    x = (screen_width - window_width) // 2
    y = (screen_height - window_height) // 2
    
    _window = webview.create_window(
        title='Hidden',
        html=HTML,
        js_api=api,
        width=window_width,
        height=window_height,
        x=x,
        y=y,
        resizable=True,
        frameless=True,
        easy_drag=True,
        background_color='#000000',
    )
    webview.start(debug=False)


if __name__ == '__main__':
    main()
