"""Semantic process exit codes for CLI tools.

Follows the CLI wrapper specification:
  0 — success
  1 — business / task error (plan rejected, task failed, verification failed)
  2 — parameter / usage error (invalid args, missing params, path not found)
  3 — system / infrastructure error (git failure, API failure, disk full)
"""

EX_OK = 0
EX_ERROR = 1
EX_USAGE = 2
EX_SYSTEM = 3
