#!/bin/bash
set -e

echo "Starting CPB local environment..."

# Auto-create virtual environment if not running in Docker
if [ ! -f "/.dockerenv" ]; then
    if [ ! -d "venv" ]; then
        echo "Creating virtual environment..."
        python3 -m venv venv
    fi
    source venv/bin/activate
fi

# Install requirements
echo "Installing/updating dependencies..."
python3 -m pip install -q -r requirements.txt || python3 -m pip install -q -r requirements.txt --break-system-packages || echo "Warning: pip install failed, continuing anyway..."


# Start Flask app
echo "Starting Flask App (app.py)..."
python3 app.py &
APP_PID=$!

echo ""
echo "=========================================================="
echo "SERVICES RUNNING!"
echo "Flask App PID: $APP_PID"
echo ""
echo "Press Ctrl+C to stop services."
echo "=========================================================="

# Wait for process
trap "echo 'Stopping services...'; kill $APP_PID; exit" SIGINT SIGTERM
wait
