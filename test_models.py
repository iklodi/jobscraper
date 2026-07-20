import os
from dotenv import load_dotenv
from google import genai

load_dotenv()
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

models_to_test = ['gemini-3-flash-preview', 'gemini-omni-flash-preview']

for m in models_to_test:
    try:
        response = client.models.generate_content(
            model=m,
            contents='Test'
        )
        print(f"{m} OK!")
    except Exception as e:
        print(f"Error with {m}: {e}")
