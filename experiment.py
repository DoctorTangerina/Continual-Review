"""Run without installing: python experiment.py --help."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from ef_review.cli import main

if __name__ == "__main__":
    main()
