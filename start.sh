#!/bin/bash
echo "Installing dependencies..."
pip3 install flask requests
echo "Starting the GitHub tracker..."
python3 app.py