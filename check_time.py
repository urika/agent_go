import time

# Convert 14:36 CST to epoch
t14 = time.strptime('2026-07-25 14:36:00', '%Y-%m-%d %H:%M:%S')
print('14:36 CST epoch:', int(time.mktime(t14)))

# Convert 01:58:40 CST to epoch
t01 = time.strptime('2026-07-25 01:58:40', '%Y-%m-%d %H:%M:%S')
print('01:58:40 CST epoch:', int(time.mktime(t01)))

# Check pytest timestamp
print('pytest timestamp:', 1784915920)
print('pytest time:', time.ctime(1784915920))

# Calculate time difference
print('diff from 14:36:', int(time.mktime(t14)) - 1784915920, 'seconds')
