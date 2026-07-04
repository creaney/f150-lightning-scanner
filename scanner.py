"""
Shim: delegates to the scanner package.
Keeps 'python3 scanner.py' working while the package is the real implementation.
"""
from scanner.__main__ import main

if __name__ == "__main__":
    main()
