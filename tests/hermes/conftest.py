"""Exercise deployable helpers with isolated configuration and fake transports."""
from pathlib import Path
import sys
SOURCE=Path(__file__).resolve().parents[2]/'deploy'/'hermes'
sys.path.insert(0,str(SOURCE))
