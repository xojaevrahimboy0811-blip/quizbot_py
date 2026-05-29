import os
import json
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List
from google import genai
from google.genai import types

app = FastAPI()

# Allow your Telegram Mini App to talk to this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ai_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

class QuizQuestion(BaseModel):
    question: str
    options: List[str] = Field(min_items=4, max_items=4)
    correct_index: int

class QuizSchema(BaseModel):
    questions: List[QuizQuestion]

class DocumentInput(BaseModel):
    text: str

@app.get("/")
def home():
    return {"status": "online"}

@app.post("/quiz")
async def extract_quiz(payload: DocumentInput):
    prompt = (
        "Extract all multiple choice questions from this Uzbek test file. "
        "Locate the correct choices using answer markings (+, *, bold, or an answer key) and set correct_index (0=A, 1=B, 2=C, 3=D)."
    )
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[prompt, payload.text],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=QuizSchema,
                temperature=0.1
            ),
        )
        parsed_json = json.loads(response.text)
        return parsed_json.get("questions", [])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
