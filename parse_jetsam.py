import json

with open('/Library/Logs/DiagnosticReports/JetsamEvent-2026-07-25-143652.ips') as f:
    lines = f.readlines()
    # Skip first line (header JSON), join rest
    data = json.loads(''.join(lines[1:]))

print("=== Coalition 10427 processes ===")
for p in data['processes']:
    if p.get('coalition') == 10427:
        name = p.get('name', '?')
        pid = p.get('pid', 0)
        lm = p.get('lifetimeMax', 0)
        cpu = p.get('cpuTime', 0)
        mb = round(lm * 16384 / 1024 / 1024, 1)
        print(f"{name:30s} pid={pid:6d}  peak={mb:8.1f}MB  cpu={cpu:8.1f}s")

print("\n=== Coalition 10491 processes (ZCode main app) ===")
for p in data['processes']:
    if p.get('coalition') == 10491:
        name = p.get('name', '?')
        pid = p.get('pid', 0)
        lm = p.get('lifetimeMax', 0)
        cpu = p.get('cpuTime', 0)
        mb = round(lm * 16384 / 1024 / 1024, 1)
        print(f"{name:30s} pid={pid:6d}  peak={mb:8.1f}MB  cpu={cpu:8.1f}s")
