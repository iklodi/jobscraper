#!/bin/bash
echo "Starting Job Scraper Dashboard..."

# Open browser (macOS/Linux)
if command -v open > /dev/null; then
    open http://localhost:5050
elif command -v xdg-open > /dev/null; then
    xdg-open http://localhost:5050
fi

# Start server
source venv/bin/activate
python app.py
