#!/bin/bash

# Setup script for T CrB monitor

echo "Setting up T CrB monitoring environment..."

# Check if Python 3.9 is available
if command -v python3.9 &> /dev/null; then
    PYTHON_VERSION="python3.9"
elif command -v python3.8 &> /dev/null; then
    PYTHON_VERSION="python3.8"
else
    echo "Error: Python 3.8 or 3.9 is required but not found."
    echo "Please install Python 3.9 using pyenv or your system package manager."
    exit 1
fi

echo "Using Python: $($PYTHON_VERSION --version)"

# Create virtual environment
echo "Creating virtual environment..."
$PYTHON_VERSION -m venv tcrb-env

# Activate virtual environment
echo "Activating virtual environment..."
source tcrb-env/bin/activate

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

echo "Setup complete!"
echo ""
echo "Next steps:"
echo "1. Edit .env file with your Gmail app password"
echo "2. Activate the environment: source tcrb-env/bin/activate"
echo "3. Test the script: python tcrb_monitor_latest.py --threshold 10.2 --interval 1"
