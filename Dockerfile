FROM mcr.microsoft.com/playwright/python:v1.63.0-jammy

WORKDIR /app

# Copy dependencies and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files into the image
COPY . .

# Run the Python script with unbuffered output (-u)
CMD ["python", "-u", "nse_announcements_bot.py"]