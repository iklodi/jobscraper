import os
from groq import Groq
from dotenv import load_dotenv

load_dotenv()
client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

prompt = "A" * 15000 # Send a large context to see if it hits a TPM/TPD limit

try:
    response = client.chat.completions.create(
        model='llama-3.1-8b-instant',
        messages=[{"role": "user", "content": prompt}],
    )
    print("llama-3.1-8b-instant OK!")
except Exception as e:
    print(f"Error: {e}")
