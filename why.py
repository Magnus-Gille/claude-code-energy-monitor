"""Checkout shim: `python3 why.py ...` runs tokenatlas.why. Not packaged."""
from tokenatlas.why import *  # noqa: F401,F403
from tokenatlas.why import main

if __name__ == '__main__':
    raise SystemExit(main())
