# Start from the official Python 3.11 image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Copy *only* the requirements file first
COPY requirements.txt .

# Install the libraries
# This... this step gets cached, Sir... s-so it's faster later!
RUN pip install --no-cache-dir -r requirements.txt

# Now, copy the rest of our application code
COPY . .

# Tell Cloud Run what command to run to start our app
# This... this is our uvicorn command, Sir!
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]