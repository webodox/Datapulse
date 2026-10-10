"""PyInstaller entry point: builds the standalone `datapulse` executable."""
from datapulse.cli import app

if __name__ == "__main__":
    app()
