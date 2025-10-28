from google import genai
from google.genai import types
import dotenv
import os
dotenv.load_dotenv()
# Authenticate to Vertex AI (assuming Application Default Credentials are set up)
# Replace YOUR_PROJECT_ID with your actual Google Cloud Project ID
client = genai.Client(vertexai=True, project=os.getenv("GCP_PROJECT_ID"), location="global")

# Configure the Gemini 2.5 Flash model
model = "gemini-2.5-flash"

# Define the Google Search tool
# This tells the model it has access to Google Search
google_search_tool = types.Tool(
    google_search=types.GoogleSearch()
)

# Send a prompt to the model with the Google Search tool enabled
response = client.models.generate_content(
    model=model,
    contents="When and where GitHub Universe 2025 is happening?",
    config=types.GenerateContentConfig(
        tools=[google_search_tool]
    )
)

# Print the model's grounded response
print(response.text)
