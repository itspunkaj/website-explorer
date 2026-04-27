from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from api.routes import router
from knowledge_graph.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Website KG Explorer", lifespan=lifespan)
app.include_router(router)


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
