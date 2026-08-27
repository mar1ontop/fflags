import pymem
import pymem.process
import struct
import re
from pathlib import Path
import sys
from datetime import datetime
import ctypes
from ctypes import wintypes
import os
import time
import json

MIN_USER_PTR = 0x10000
MAX_USER_PTR = 0x7FFFFFFFFFFF
MAX_LIST_NODES = 250_000

def valid_ptr(value):
    return MIN_USER_PTR <= value <= MAX_USER_PTR

class Memory:
    pm = None
    process_id = 0
    base_address = 0

    @classmethod
    def get_pid(cls, process_name):
        try:
            processes = pymem.process.process_from_name(process_name)
            if processes:
                return processes.th32ProcessID
            return 0
        except:
            return 0

    @classmethod
    def attach_to_process(cls, pid):
        try:
            cls.pm = pymem.Pymem()
            cls.pm.open_process_from_id(pid)
            cls.process_id = pid
            return True
        except:
            return False

    @classmethod
    def get_module_base_address(cls, module_name):
        try:
            module = pymem.process.module_from_name(cls.pm.process_handle, module_name)
            if module:
                return module.lpBaseOfDll
        except:
            pass
        return 0

    @classmethod
    def read(cls, address, size=8):
        try:
            return cls.pm.read_bytes(address, size)
        except:
            return None

    @classmethod
    def read_uintptr(cls, address):
        try:
            return struct.unpack('Q', cls.pm.read_bytes(address, 8))[0]
        except:
            return 0

    @classmethod
    def read_int(cls, address):
        try:
            return struct.unpack('i', cls.pm.read_bytes(address, 4))[0]
        except:
            return 0

    @classmethod
    def read_uint(cls, address):
        try:
            return struct.unpack('I', cls.pm.read_bytes(address, 4))[0]
        except:
            return 0

    @classmethod
    def read_batch(cls, address, size):
        try:
            return cls.pm.read_bytes(address, size)
        except:
            return None

    @classmethod
    def read_string(cls, address):
        try:
            length = cls.read_int(address + 0x18)
            if length <= 0 or length > 1000:
                return ""
            
            data_address = address
            if length >= 16:
                data_address = cls.read_uintptr(address)
                if not data_address:
                    return ""
            
            data = cls.read_batch(data_address, length)
            if data:
                result = data.decode('utf-8', errors='ignore').split('\x00')[0]
                return result
            return ""
        except:
            return ""

    @classmethod
    def get_roblox_version(cls):
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            ctypes.windll.kernel32.QueryFullProcessImageNameW(
                cls.pm.process_handle, 0, buf, ctypes.byref(size))
            exe_path = buf.value
            folder = os.path.basename(os.path.dirname(exe_path))
            
            if folder.startswith("version-"):
                return folder
            
            match = re.search(r'version-([a-f0-9]{16})', folder)
            if match:
                return match.group(0)
            
            parent_folder = os.path.basename(os.path.dirname(os.path.dirname(exe_path)))
            if parent_folder.startswith("version-"):
                return parent_folder
            
            match = re.search(r'version-([a-f0-9]{16})', exe_path)
            if match:
                return match.group(0)
        except:
            pass
        return "unknown"

class Offsets:
    FFlagList = 0x0
    ValueGetSet = 0x30
    FlagToValue = 0x0

def format_number(num):
    return f"{num:,}"

def clean_name(name):
    return re.sub(r'[^a-zA-Z0-9]', '_', name)

def save_hpp(flags_data, roblox_version, offsets, flag_count, output_path="Offsets.hpp"):
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(f"// Roblox Version - {roblox_version}\n")
        f.write(f"// Total flags: {format_number(flag_count)}\n")
        f.write(f"// Dumped by syscall at {current_time}\n\n")
        f.write("#pragma once\n\n")
        f.write("namespace FFlagList\n{\n")
        f.write(f"    uintptr_t Pointer = 0x{offsets.FFlagList:X};\n")
        f.write(f"    uintptr_t ToFlag = 0x{offsets.ValueGetSet:X};\n")
        f.write(f"    uintptr_t ToValue = 0x{offsets.FlagToValue:X};\n")
        f.write("}\n\n")
        f.write("namespace FFlags\n{\n")
        
        for flag_name, flag_offset in flags_data:
            f.write(f"    uintptr_t {flag_name} = 0x{flag_offset:X};\n")
        
        f.write("}\n")
    
def save_json(flags_data, roblox_version, offsets, flag_count, output_path="Offsets.json"):
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    data = {
        "Miscellaneous": {
            "Roblox Version": roblox_version,
            "FFlag Count": flag_count,
            "Dumped by syscall at": current_time
        },
        "FFlagList": {
            "Pointer": f"0x{offsets.FFlagList:X}",
            "ToFlag": f"0x{offsets.ValueGetSet:X}",
            "ToValue": f"0x{offsets.FlagToValue:X}"
        },
        "FFlags": {}
    }
    
    for flag_name, flag_offset in flags_data:
        data["FFlags"][flag_name] = f"0x{flag_offset:X}"
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    
def save_csharp(flags_data, roblox_version, offsets, flag_count, output_path="Offsets.cs"):
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(f"// Roblox Version - {roblox_version}\n")
        f.write(f"// Total flags: {format_number(flag_count)}\n")
        f.write(f"// Dumped by syscall at {current_time}\n\n")
        f.write("using System;\n\n")
        f.write("public static class FFlagList\n{\n")
        f.write(f"    public const long Pointer = 0x{offsets.FFlagList:X};\n")
        f.write(f"    public const long ToFlag = 0x{offsets.ValueGetSet:X};\n")
        f.write(f"    public const long ToValue = 0x{offsets.FlagToValue:X};\n")
        f.write("}\n\n")
        f.write("public static class FFlags\n{\n")
        
        for flag_name, flag_offset in flags_data:
            f.write(f"    public const long {flag_name} = 0x{flag_offset:X};\n")
        
        f.write("}\n")
    
def main():
    print("\nsyscall's FFlag Dumper")
    print("------------------------")
    print()
    
    pid = Memory.get_pid("RobloxPlayerBeta.exe")
    if not Memory.attach_to_process(pid):
        print("[!] Couldn't attach to Roblox!")
        input("\nPress Enter to Exit...")
        return 1
    
    Memory.base_address = Memory.get_module_base_address("RobloxPlayerBeta.exe")
    roblox_version = Memory.get_roblox_version()
    
    print(f" | Attached to Roblox (PID: {pid})")
    print(f" | Roblox Version: {roblox_version}")
    
    found_map = False
    scan_start = 0x7000000
    scan_end = 0x9000000
    chunk_size = 0x1000
    
    print(" | Scanning for FFlag Container...")
    
    for current_offset in range(scan_start, scan_end, chunk_size):
        if found_map:
            break
        
        bytes_to_read = min(chunk_size, scan_end - current_offset)
        buffer = Memory.read_batch(Memory.base_address + current_offset, bytes_to_read)
        if not buffer:
            continue
        
        for i in range(0, len(buffer) - 7, 8):
            maybe_map = struct.unpack_from('<Q', buffer, i)[0]
            
            if not valid_ptr(maybe_map):
                continue
            
            is_this_really_a_map_validation = Memory.read_uintptr(maybe_map)
            if not valid_ptr(is_this_really_a_map_validation):
                continue
            
            if is_this_really_a_map_validation == 0x3F800000:
                wow = current_offset + i
                
                map_start = Memory.read_uintptr(maybe_map + 0x8)
                if not valid_ptr(map_start):
                    continue
                map_end = Memory.read_uintptr(map_start + 0x8)
                current = Memory.read_uintptr(map_start)
                if not valid_ptr(map_end) or not valid_ptr(current):
                    continue

                visited = set()
                nodes = 0
                if current < 0x10000 or current > 0x7FFFFFFFFFFF:
                    continue
                
                while current != 0 and current != map_end and current not in visited and nodes < MAX_LIST_NODES:
                    visited.add(current)
                    nodes += 1
                    name = Memory.read_string(current + 0x10)
                    if name == "BatchThumbnailMinWaitMs":
                        for value_get_set_offset in range(0x20, 0x50, 0x8):
                            test_value_get_set = Memory.read_uintptr(current + value_get_set_offset)
                            if not valid_ptr(test_value_get_set):
                                continue
                            
                            for flag_to_value_offset in range(0x0, 0x100, 0x8):
                                test_pointer = Memory.read_uintptr(test_value_get_set + flag_to_value_offset)
                                if not valid_ptr(test_pointer):
                                    continue
                                
                                try:
                                    test_value = Memory.read_int(test_pointer)
                                    if test_value == 15:
                                        found_map = True
                                        Offsets.FFlagList = wow
                                        Offsets.ValueGetSet = value_get_set_offset
                                        Offsets.FlagToValue = flag_to_value_offset
                                        break
                                except:
                                    continue
                            
                            if found_map:
                                break
                        
                        if not found_map:
                            print("[!] Couldn't locate FFlag map.")
                            input("\nPress Enter to Exit...")
                            return 1
                    
                    new_current = Memory.read_uintptr(current)
                    if not valid_ptr(new_current):
                        break
                    if current == new_current:
                        break
                    current = new_current
    
    if not found_map:
        print("[!] FFlag map not found in scan range.")
        input("\nPress Enter to Exit...")
        return 1
        
    print(" | Dumping FFlags...\n")
    fflag_pointer1 = Memory.read_uintptr(Memory.base_address + Offsets.FFlagList)
    fflag_list = Memory.read_uintptr(fflag_pointer1 + 0x8)
    
    last = Memory.read_uintptr(fflag_list + 0x8)
    current = fflag_list
    
    temp_flags = []
    seen_names = set()
    flag_count = 0
    
    visited = set()
    while current != 0 and current != last and current not in visited and len(visited) < MAX_LIST_NODES:
        visited.add(current)
        name = Memory.read_string(current + 0x10)
        value_get_set = Memory.read_uintptr(current + Offsets.ValueGetSet)
        
        if value_get_set < 0x10000 or value_get_set > 0x7FFFFFFFFFFF:
            current = Memory.read_uintptr(current)
            continue
        
        mhm = Memory.read_string(value_get_set + Offsets.FlagToValue)
        
        if mhm != "True" and mhm != "False":
            offset = Memory.read_uintptr(value_get_set + Offsets.FlagToValue) - Memory.base_address
            
            # Offsets are module-relative and must not become negative or
            # point outside the user address space.
            if offset < 0 or not valid_ptr(offset + Memory.base_address):
                current = Memory.read_uintptr(current)
                continue
            
            clean_flag_name = clean_name(name)
            if clean_flag_name and clean_flag_name not in seen_names:
                seen_names.add(clean_flag_name)
                temp_flags.append((clean_flag_name, offset))
                flag_count += 1
        
        current = Memory.read_uintptr(current)
    
    save_hpp(temp_flags, roblox_version, Offsets, flag_count, "Offsets.hpp")
    save_json(temp_flags, roblox_version, Offsets, flag_count, "Offsets.json")
    save_csharp(temp_flags, roblox_version, Offsets, flag_count, "Offsets.cs")
    
    print(f" | FFlags: {format_number(flag_count)}")
    print(f" | FFlagList:")
    print(f" | Pointer: 0x{Offsets.FFlagList:X}")
    print(f" | ToFlag: 0x{Offsets.ValueGetSet:X}")
    print(f" | ToValue: 0x{Offsets.FlagToValue:X}")
    print("\n | Saved to:")
    print("  - Offsets.hpp (C++)")
    print("  - Offsets.json (JSON)")
    print("  - Offsets.cs (C#)\n")
    
    input("Press Enter to exit...")
    return 0

if __name__ == "__main__":
    sys.exit(main())
