"""Launch from a source checkout without requiring an editable install."""

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

if __name__ == "__main__":
    import tkinter as tk

    from image_agent.desktop import DesktopApp

    root = tk.Tk()
    DesktopApp(root, project_dir=PROJECT)
    root.mainloop()
