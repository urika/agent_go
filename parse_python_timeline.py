import json

with open('/Library/Logs/DiagnosticReports/JetsamEvent-2026-07-25-143652.ips') as f:
    lines = f.readlines()
    data = json.loads(''.join(lines[1:]))

# Incident time: 2026-07-25 14:36:52 CST
# age is in nanoseconds
incident_ts = 1784961412  # epoch for 2026-07-25 14:36:52 CST

print('=== Python process timeline (reverse calculated start time) ===')
for p in data['processes']:
    name = p.get('name', '?')
    if name != 'Python':
        continue
    pid = p.get('pid', 0)
    age_ns = p.get('age', 0)
    age_sec = age_ns / 1e9
    age_min = age_sec / 60
    age_hr = age_min / 60
    start_ts = incident_ts - age_sec
    import time
    start_time = time.ctime(start_ts)
    lm = p.get('lifetimeMax', 0)
    cpu = p.get('cpuTime', 0)
    mb = round(lm * 16384 / 1024 / 1024, 1)
    print(f'pid={pid:6d}  start={start_time}  age={age_hr:.1f}h  peak={mb:8.1f}MB  cpu={cpu:7.1f}s')

print('\n=== zsh process timeline ===')
for p in data['processes']:
    name = p.get('name', '?')
    if name != 'zsh':
        continue
    pid = p.get('pid', 0)
    age_ns = p.get('age', 0)
    age_sec = age_ns / 1e9
    start_ts = incident_ts - age_sec
    start_time = time.ctime(start_ts)
    print(f'pid={pid:6d}  start={start_time}  age={age_sec/60:.1f}min')

print('\n=== tail process timeline ===')
for p in data['processes']:
    name = p.get('name', '?')
    if name != 'tail':
        continue
    pid = p.get('pid', 0)
    age_ns = p.get('age', 0)
    age_sec = age_ns / 1e9
    start_ts = incident_ts - age_sec
    start_time = time.ctime(start_ts)
    print(f'pid={pid:6d}  start={start_time}  age={age_sec/60:.1f}min')
