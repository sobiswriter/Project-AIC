import vertexai
from vertexai.generative_models import GenerativeModel, Tool, FunctionDeclaration, Part

from main import GCP_PROJECT_ID

# Replace with your project ID and a supported location.
PROJECT_ID = GCP_PROJECT_ID
LOCATION = "us-central1"

# Initialize Vertex AI SDK
vertexai.init(project=PROJECT_ID, location=LOCATION)

# Initialize the Gemini model with the specified version
model_id = "gemini-2.5-flash"
model = GenerativeModel(model_id)

# Define the Google Search tool using a function declaration
google_search_tool = Tool(
    function_declarations=[
        FunctionDeclaration(
            name="google_search",
            description="Searches Google for a specific query.",
            parameters={} # No parameters are needed for the basic search
        )
    ]
)

# Define the content to be sent, including the prompt and the tool
contents = [
    Part.from_text("When and where GitHub Universe 2025 is happening?"),
]

# Send the request with the tools enabled
response = model.generate_content(
    contents=contents,
    tools=[google_search_tool]
)

# Print the response, which will be grounded with web search results
print(response.text)
