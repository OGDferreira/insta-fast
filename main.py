from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers.scraper import router as scraper_router


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(scraper_router)


@app.get("/")
def read_root() -> dict[str, str]:
    return {"status": "online", "service": "Insta Fast API"}
