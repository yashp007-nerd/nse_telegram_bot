FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# Copy requirement list and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files into the container
COPY . .

# Run the Python script with unbuffered output (-u) so logs appear in real-time on Render
CMD ["python", "-u", "nse_announcements_bot.py"]