
import sys
print(f"Python: {sys.executable}")
print(f"Path: {sys.path}")
try:
    import channels
    print(f"Channels found: {channels.__file__}")
except ImportError as e:
    print(f"ImportError: {e}")
except Exception as e:
    print(f"Error: {e}")
