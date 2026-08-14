from fastapi import FastAPI

app = FastAPI(title="Exam Quiz Bot Health API")


@app.get("/")
async def root():
    return {"status": "ok", "service": "exam-quiz-bot"}


@app.get("/health")
async def health():
    return {"status": "healthy"}
