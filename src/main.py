from fastapi import FastAPI, Query
from pydantic import BaseModel, Field

from src.automation.browser_search import search_and_scrape

app = FastAPI(
    title="LeadGen - Google AI Mode Search",
    version="1.0.0",
    description="Google AI Mode lead search automation.",
)


class AutoSearchRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=500)
    max_results: int = Field(default=8, ge=1, le=20)


@app.post("/search/auto")
async def search_auto_endpoint(req: AutoSearchRequest):
    return await search_and_scrape(req.query, max_results=req.max_results)


@app.get("/search/auto")
async def search_auto_get(
    q: str = Query(..., min_length=2, max_length=500),
    max_results: int = Query(8, ge=1, le=20),
):
    return await search_and_scrape(q, max_results=max_results)


@app.get("/health")
def health():
    return {"status": "ok", "engine": "google-ai"}
